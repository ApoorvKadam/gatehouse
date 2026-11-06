"""A facility management system simulator shaped after Cubby's published Operator API.

It exists so the desk can be exercised against an FMS that behaves like a real one does on a bad day:
declines that come back as HTTP 200, payments whose outcome is unknown, responses lost after the money
was applied, ledger updates that lag the payment record, and payments reversed days later.
"""
