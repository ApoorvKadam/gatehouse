"""Access codes, locks, tasks and delinquency exemptions, in the shapes of Cubby's Operator API.

Validation is per endpoint, as Cubby documents it: Lock Search takes exactly one of organizationId or leaseId and
answers 403 otherwise; Task Search takes exactly one scope; Tasks Create takes a customer or a unit, not both; a task
moves OPEN -> COMPLETED or ARCHIVED and back only through OPEN. The /sim routes are what staff or devices would do in
a real facility (attach or return a lock, complete a task) and the named faults; Gatehouse never calls them."""
from __future__ import annotations

from datetime import date

from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .store import (
    AccessCode, AccessFault, AccessRule, ApiKey, Exemption, Facility, Lease, Lock, Task, facility_today, new_id,
    run_access_automation, unit_id_of, utcnow,
)

TASK_STATUSES = ("OPEN", "COMPLETED", "ARCHIVED")
FAULTS = ("gate_sync_stale", "lose_task_response", "lose_send_response")


def register(app: FastAPI, db, caller, own_lease, ok, iso, ApiError) -> None:

    def own_facility(session: Session, key: ApiKey, facility_id) -> Facility:
        facility = session.get(Facility, facility_id or "")
        if facility is None or facility.org_id != key.org_id:
            raise ApiError(404, "Facility not found")
        return facility

    def lock_json(lock: Lock) -> dict:
        out = {"lockId": lock.lock_id, "id": lock.lock_id, "facilityId": lock.facility_id, "organizationId": lock.org_id,
               "serial": lock.serial, "combo": lock.combo, "status": lock.status}
        if lock.status in ("OPERATOR_USE", "TENANT_USE"):
            out["attachedTo"] = lock.attached_to
        if lock.status == "DISABLED":
            out["disabledReason"] = lock.disabled_reason
        return out

    def task_json(t: Task) -> dict:
        return {"id": t.task_id, "taskId": t.task_id, "title": t.title, "description": t.description,
                "dueDate": iso(t.due_date), "status": t.status, "facilityId": t.facility_id, "customerId": t.customer_id,
                "unitId": t.unit_id, "createdAt": iso(t.created_at), "updatedAt": iso(t.updated_at)}

    def exemption_json(e: Exemption) -> dict:
        return {"leaseId": e.lease_id, "reason": e.reason, "otherReason": e.other_reason, "endDate": iso(e.end_date),
                "pauseDelinquencyFees": e.pause_delinquency_fees, "pauseOverlock": e.pause_overlock,
                "pauseLienEligibility": e.pause_lien_eligibility, "pauseCollectionsMessages": e.pause_collections_messages,
                "note": e.note}

    # ---------------- access codes ----------------
    @app.post("/v1/access-codes/search")
    def access_codes_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        where = body.get("where") or {}
        if not where.get("facilityId"):
            raise ApiError(400, "facilityId is required")
        facility = own_facility(session, key, where["facilityId"])
        q = select(AccessCode).where(AccessCode.facility_id == facility.facility_id)
        if where.get("customerId"):
            q = q.where(AccessCode.customer_id == where["customerId"])
        rows = session.scalars(q.order_by(AccessCode.code_id)).all()
        return ok({"accessCodes": [{"code": c.code, "enabled": c.enabled, "customerId": c.customer_id} for c in rows]})

    @app.post("/v1/access-codes/check")
    def access_codes_check(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        if not body.get("facilityId") or not body.get("code"):
            raise ApiError(400, "facilityId and code are required")
        facility = own_facility(session, key, body["facilityId"])
        code = session.scalar(select(AccessCode).where(AccessCode.facility_id == facility.facility_id,
                                                       AccessCode.code == str(body["code"])))
        if code is None:
            return ok({"accessAllowed": False, "message": "Code not found"})
        if not code.enabled:
            return ok({"accessAllowed": False, "message": "Code disabled"})
        return ok({"accessAllowed": True, "message": "Access allowed"})

    # ---------------- locks ----------------
    @app.post("/v1/locks/search")
    def locks_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        # the where wrapper wins; the deprecated top-level form still works
        where = body.get("where") if isinstance(body.get("where"), dict) else {k: body[k] for k in ("organizationId", "leaseId") if k in body}
        scopes = [k for k in ("organizationId", "leaseId") if where.get(k)]
        if len(scopes) != 1:
            raise ApiError(403, "Exactly one of organizationId or leaseId is required")
        if scopes[0] == "organizationId":
            if where["organizationId"] != key.org_id:
                raise ApiError(403, "Not permitted for this organization")
            rows = session.scalars(select(Lock).where(Lock.org_id == key.org_id).order_by(Lock.lock_id)).all()
        else:
            lease = own_lease(session, key, where["leaseId"])
            rows = session.scalars(select(Lock).where(Lock.attached_to == unit_id_of(lease),
                                                      Lock.status.in_(("OPERATOR_USE", "TENANT_USE"))).order_by(Lock.lock_id)).all()
        return ok({"locks": [lock_json(l) for l in rows]})

    # ---------------- tasks ----------------
    @app.post("/v1/tasks/create")
    def tasks_create(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        facility = own_facility(session, key, body.get("facilityId"))
        if not body.get("title"):
            raise ApiError(400, "title is required")
        if body.get("customerId") and body.get("unitId"):
            raise ApiError(400, "A task belongs to a customer or a unit, not both")
        due = body.get("dueDate")
        task = Task(task_id=new_id("task"), facility_id=facility.facility_id, title=body["title"],
                    description=body.get("description"), due_date=date.fromisoformat(due) if due else None,
                    customer_id=body.get("customerId"), unit_id=body.get("unitId"))
        session.add(task)
        session.commit()
        lose = session.scalar(select(AccessFault).where(AccessFault.kind == "lose_task_response",
                                                        AccessFault.facility_id == facility.facility_id))
        if lose is not None:
            # the task exists; the answer carrying its id never arrives complete
            def broken():
                yield b'{"status": 200, "data": {"taskId": '
                raise ConnectionResetError("connection dropped after creating the task")
            return StreamingResponse(broken(), media_type="application/json")
        return ok({"taskId": task.task_id})

    @app.post("/v1/tasks/search")
    def tasks_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        where = body.get("where") or {}
        scopes = [k for k in ("taskId", "organizationId", "facilityId", "unitId", "customerId") if where.get(k)]
        if len(scopes) != 1:
            raise ApiError(400, "Exactly one of taskId, organizationId, facilityId, unitId or customerId is required")
        q = select(Task).join(Facility, Facility.facility_id == Task.facility_id).where(Facility.org_id == key.org_id)
        column = {"taskId": Task.task_id, "facilityId": Task.facility_id, "unitId": Task.unit_id,
                  "customerId": Task.customer_id}.get(scopes[0])
        if column is not None:
            q = q.where(column == where[scopes[0]])
        elif where["organizationId"] != key.org_id:
            raise ApiError(403, "Not permitted for this organization")
        if where.get("status"):
            if where["status"] not in TASK_STATUSES:
                raise ApiError(400, f"status must be one of {', '.join(TASK_STATUSES)}")
            q = q.where(Task.status == where["status"])
        return ok({"tasks": [task_json(t) for t in session.scalars(q.order_by(Task.created_at, Task.task_id)).all()]})

    def change_task(session: Session, task: Task, body: dict) -> Task:
        status = body.get("status")
        edits = {k: v for k, v in body.items() if k not in ("taskId", "status")}
        if status is not None and status not in TASK_STATUSES:
            raise ApiError(400, f"status must be one of {', '.join(TASK_STATUSES)}")
        if edits and task.status != "OPEN" and status != "OPEN":
            raise ApiError(400, "Only an OPEN task can be edited")
        if status and status != task.status and task.status != "OPEN" and status != "OPEN":
            raise ApiError(400, "A task must pass back through OPEN to move between COMPLETED and ARCHIVED")
        if "title" in edits:
            task.title = edits["title"]
        if "description" in edits:
            task.description = edits["description"]
        if status:
            task.status = status
        task.updated_at = utcnow()
        session.commit()
        return task

    @app.post("/v1/tasks/update")
    def tasks_update(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        task = session.get(Task, body.get("taskId") or "")
        if task is None or session.get(Facility, task.facility_id).org_id != key.org_id:
            raise ApiError(404, "Task not found")
        return ok({"task": task_json(change_task(session, task, body))})

    # ---------------- delinquency exemptions ----------------
    @app.post("/v1/delinquency-exemptions/search")
    def exemptions_search(body: dict, key: ApiKey = Depends(caller), session: Session = Depends(db)):
        where = body.get("where") or {}
        scopes = [k for k in ("leaseId", "customerId", "facilityId") if where.get(k)]
        if len(scopes) != 1:
            raise ApiError(400, "Exactly one of leaseId, customerId or facilityId is required")
        if scopes[0] == "leaseId":
            leases = [own_lease(session, key, where["leaseId"])]
        else:
            q = select(Lease).join(Facility, Facility.facility_id == Lease.facility_id).where(Facility.org_id == key.org_id)
            q = q.where(Lease.customer_id == where["customerId"]) if scopes[0] == "customerId" else q.where(Lease.facility_id == where["facilityId"])
            leases = session.scalars(q).all()
        out = []
        for lease in leases:
            today = facility_today(session, lease.facility_id)
            e = session.scalar(select(Exemption).where(Exemption.lease_id == lease.lease_id, Exemption.cancelled.is_(False),
                                                       Exemption.end_date >= today))
            if e is not None:
                out.append(exemption_json(e))
        return ok({"delinquencyExemptions": out})

    # ---------------- what staff, devices and the daily run do (test controls) ----------------
    def control(session: Session, model, ident, what: str):
        row = session.get(model, ident or "")
        if row is None:
            raise ApiError(404, f"{what} not found")
        return row

    @app.post("/sim/locks/attach")
    def sim_lock_attach(body: dict, session: Session = Depends(db)):
        """A person puts a lock on a unit: OPERATOR_USE restricts it (an overlock), TENANT_USE rents it to the tenant."""
        lock = control(session, Lock, body.get("lockId"), "Lock")
        lease = control(session, Lease, body.get("leaseId"), "Lease")
        if lock.status != "AVAILABLE":
            raise ApiError(400, "Only an available lock can be attached")
        lock.status, lock.attached_to = body.get("lockUse", "OPERATOR_USE"), unit_id_of(lease)
        session.commit()
        return ok({"lock": lock_json(lock)})

    @app.post("/sim/locks/return")
    def sim_lock_return(body: dict, session: Session = Depends(db)):
        """A person takes a lock off a unit and returns it to stock, and records it."""
        lock = control(session, Lock, body.get("lockId"), "Lock")
        if lock.status not in ("OPERATOR_USE", "TENANT_USE"):
            raise ApiError(400, "Only an attached lock can be returned")
        lock.status, lock.attached_to = "AVAILABLE", None
        session.commit()
        return ok({"lock": lock_json(lock)})

    @app.post("/sim/tasks/complete")
    def sim_task_complete(body: dict, session: Session = Depends(db)):
        """A person marks a task done in the FMS app, whatever they actually did."""
        task = control(session, Task, body.get("taskId"), "Task")
        return ok({"task": task_json(change_task(session, task, {"taskId": task.task_id, "status": body.get("status", "COMPLETED")}))})

    @app.post("/sim/exemptions")
    def sim_exemption(body: dict, session: Session = Depends(db)):
        """Staff exempt a lease from delinquency automation (Delinquency Exemptions Create)."""
        lease = control(session, Lease, body.get("leaseId"), "Lease")
        today = facility_today(session, lease.facility_id)
        if session.scalar(select(Exemption).where(Exemption.lease_id == lease.lease_id, Exemption.cancelled.is_(False),
                                                  Exemption.end_date >= today)):
            raise ApiError(400, "This lease already has an active exemption")
        try:
            end = date.fromisoformat(body.get("endDate") or "")
        except ValueError as exc:
            raise ApiError(400, "endDate must be YYYY-MM-DD") from exc
        if end <= today:
            raise ApiError(400, "endDate must be after today")
        flags = {k: bool(body.get(k)) for k in ("pauseDelinquencyFees", "pauseOverlock", "pauseLienEligibility", "pauseCollectionsMessages")}
        if not any(flags.values()):
            raise ApiError(400, "At least one pause must be true")
        e = Exemption(exemption_id=new_id("dex"), lease_id=lease.lease_id, reason=body.get("reason", "OTHER"),
                      other_reason=body.get("otherReason"), end_date=end, note=body.get("note"),
                      pause_delinquency_fees=flags["pauseDelinquencyFees"], pause_overlock=flags["pauseOverlock"],
                      pause_lien_eligibility=flags["pauseLienEligibility"], pause_collections_messages=flags["pauseCollectionsMessages"])
        session.add(e)
        session.flush()
        run_access_automation(session, lease.facility_id, lease.customer_id)
        session.commit()
        return ok({"delinquencyExemption": exemption_json(e)})

    @app.post("/sim/exemptions/cancel")
    def sim_exemption_cancel(body: dict, session: Session = Depends(db)):
        lease = control(session, Lease, body.get("leaseId"), "Lease")
        for e in session.scalars(select(Exemption).where(Exemption.lease_id == lease.lease_id, Exemption.cancelled.is_(False))):
            e.cancelled = True
        session.flush()
        run_access_automation(session, lease.facility_id, lease.customer_id)
        session.commit()
        return ok({"cancelled": True})

    @app.post("/sim/access/run")
    def sim_access_run(body: dict | None = None, session: Session = Depends(db)):
        """The FMS's daily delinquency run, for every customer at a facility."""
        facility = control(session, Facility, (body or {}).get("facilityId"), "Facility")
        for customer_id in {l.customer_id for l in session.scalars(select(Lease).where(Lease.facility_id == facility.facility_id))}:
            run_access_automation(session, facility.facility_id, customer_id)
        session.commit()
        return ok({"ran": True})

    @app.post("/sim/access/rule")
    def sim_access_rule(body: dict, session: Session = Depends(db)):
        facility = control(session, Facility, body.get("facilityId"), "Facility")
        days = body.get("lockoutDays")
        rule = session.get(AccessRule, facility.facility_id)
        if days is None:
            if rule is not None:
                session.delete(rule)
        else:
            if not isinstance(days, int) or days < 0:
                raise ApiError(400, "lockoutDays must be a whole number of days")
            session.merge(AccessRule(facility_id=facility.facility_id, lockout_days=days))
        session.commit()
        return ok({"facilityId": facility.facility_id, "lockoutDays": days})

    @app.post("/sim/faults")
    def sim_fault(body: dict, session: Session = Depends(db)):
        """Switch a named fault on or off: gate_sync_stale (facilityId, customerId), lose_task_response (facilityId) or
        lose_send_response (facilityId: messages/sms/send sends the text, then the connection breaks)."""
        kind = body.get("kind")
        if kind not in FAULTS:
            raise ApiError(400, f"kind must be one of {', '.join(FAULTS)}")
        control(session, Facility, body.get("facilityId"), "Facility")
        q = select(AccessFault).where(AccessFault.kind == kind, AccessFault.facility_id == body["facilityId"])
        if kind == "gate_sync_stale":
            q = q.where(AccessFault.customer_id == body.get("customerId"))
        existing = session.scalar(q)
        if body.get("on", True) and existing is None:
            session.add(AccessFault(kind=kind, facility_id=body["facilityId"], customer_id=body.get("customerId")))
        elif not body.get("on", True) and existing is not None:
            session.delete(existing)
        session.commit()
        return ok({"kind": kind, "on": body.get("on", True)})

    @app.get("/sim/access/truth")
    def sim_access_truth(facilityId: str, session: Session = Depends(db)):
        """Everything the simulator knows about access at a facility, faults included. Reading it changes nothing."""
        facility = control(session, Facility, facilityId, "Facility")
        rule = session.get(AccessRule, facility.facility_id)
        leases = session.scalars(select(Lease).where(Lease.facility_id == facility.facility_id).order_by(Lease.lease_id)).all()
        return ok({
            "facilityId": facility.facility_id, "lockoutDays": rule.lockout_days if rule else None,
            "codes": [{"customerId": c.customer_id, "enabled": c.enabled} for c in
                      session.scalars(select(AccessCode).where(AccessCode.facility_id == facility.facility_id))],
            "leases": [{"leaseId": l.lease_id, "unitId": unit_id_of(l), "overlockNeeded": l.overlock_needed,
                        "delinquencyStartDate": iso(l.delinquency_start_date),
                        "locks": [lock_json(k) for k in session.scalars(select(Lock).where(Lock.attached_to == unit_id_of(l)))]}
                       for l in leases],
            "tasks": [task_json(t) for t in session.scalars(select(Task).where(Task.facility_id == facility.facility_id))],
            "faults": [{"kind": f.kind, "customerId": f.customer_id} for f in
                       session.scalars(select(AccessFault).where(AccessFault.facility_id == facility.facility_id))],
        })
