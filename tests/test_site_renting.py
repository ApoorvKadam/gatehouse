"""The facility website rents on the signed-in visitor's copy."""
from datetime import timedelta
import re
from html import unescape

from fastapi.testclient import TestClient

from core.seed import NOW
from core import renting
from core.billing import Ledger
from core.stripe_processor import StripeProcessor
from tests.test_fms import record_of, seen, sign_in, site
from tests.test_fms_renting_pages import desk_of, rent_of, token, vacant
from tests.test_fms_card_payments import stripe



def own(fd, ident):
    """The rentals made on this copy, without the existing tenants' rentals the year starts with."""
    return [r for r in rent_of(fd, ident).rentals.values() if not r.provenance]

def begin(c, unit, name="Rae Quinn", phone="+14075550199"):
    page = c.get(f"/rent/{unit}").text
    return c.post(f"/rent/{unit}", data={"token": token(page), "name": name, "phone": phone}, follow_redirects=False)


def agreement_hash(page):
    return re.search(r'name="sha256" value="([0-9a-f]+)"', page).group(1)


def finish(c, address, card="card_ok"):
    page = c.get(address).text
    return c.post(address, data={"token": token(page), "accept": "yes", "card": card, "sha256": agreement_hash(page)}, follow_redirects=False), page


def test_only_priced_vacant_units_are_offered(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unpriced = vacant(fd, ident)[0]
        store, stream = fd.ready(), fd.stream(ident)
        with store.write():
            led = Ledger(store.books(stream))
            led.emit('unit.priced', NOW, {'unit': unpriced, 'rent_cents': None, 'size': '10 x 10'}, 'test:unpriced')
            store.append(stream, led.pending)
        rent = rent_of(fd, ident)
        page = c.get('/rent').text
        for unit in fd.view(ident)[0].units:
            offered = rent.status(unit, NOW) == 'vacant' and rent.units[unit].rent_cents is not None
            assert (f'href="/rent/{unit}"' in page) == offered
        assert 'Northstar Storage' in page and 'Orlando, FL' in page


def test_rent_and_repeat_are_seen_by_staff(fms_url, sim, stripe):
    app = site(fms_url)
    app.state.front_desk.processor = StripeProcessor('sk_test_fake', base_url=stripe.url, clock=lambda: NOW.timestamp())
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unit = next(u for u in vacant(fd, ident, 20) if rent_of(fd, ident).units[u].rent_cents)
        start = begin(c, unit)
        assert start.status_code == 303
        result, page = finish(c, start.headers['location'])
        rental = next(iter(own(fd, ident)))
        agreement = rent_of(fd, ident).agreements[rental.agreement]
        assert agreement.version in page and all(line in unescape(page) for line in agreement.text.splitlines())
        again = c.post(start.headers['location'], data={'token': token(page), 'accept': 'yes', 'card': 'card_ok', 'sha256': agreement_hash(page)}, follow_redirects=False)
        assert again.headers['location'] == result.headers['location']
        text = seen(c.get(result.headers['location']).text)
        assert 'Active' in text and 'Paid' in text and 'Accepted by Rae Quinn' in text and 'Code set and read back' in text
        assert len(own(fd, ident)) == 1
        assert len(stripe.intents) == 1
        assert f'title="{unit}: Rae Quinn' in c.get('/sitemap').text
        assert 'Active' in seen(c.get(record_of(c, 'Rae Quinn')).text)


def test_expired_hold_and_decline(fms_url, sim, stripe):
    app = site(fms_url)
    app.state.front_desk.processor = StripeProcessor('sk_test_fake', base_url=stripe.url, clock=lambda: NOW.timestamp())
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        units = [u for u in vacant(fd, ident, 20) if rent_of(fd, ident).units[u].rent_cents]
        first = begin(c, units[0]).headers['location']
        fd.clock = lambda: NOW + timedelta(minutes=30)
        result, _ = finish(c, first)
        assert 'cancelled because the hold ran out' in seen(c.get(result.headers['location']).text).lower()
        second = begin(c, units[1]).headers['location']
        result, _ = finish(c, second, 'card_declined')
        assert 'card declined' in seen(c.get(result.headers['location']).text).lower()
        assert not any(r.state == 'active' for r in own(fd, ident))


def test_a_declined_card_can_be_tried_again_without_accepting_twice(fms_url, sim, stripe):
    app = site(fms_url)
    app.state.front_desk.processor = StripeProcessor('sk_test_fake', base_url=stripe.url, clock=lambda: NOW.timestamp())
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        units = [u for u in vacant(fd, ident, 20) if rent_of(fd, ident).units[u].rent_cents]
        path = begin(c, units[0]).headers['location']
        result, _ = finish(c, path, 'card_declined')
        assert 'try another card' in seen(c.get(result.headers['location']).text).lower()
        result, _ = finish(c, path)
        assert [r.state for r in own(fd, ident)] == ['active']
        assert 'try another card' not in seen(c.get(result.headers['location']).text).lower()


def test_one_checkout_page_pays_at_most_once_and_a_retry_needs_no_second_acceptance(fms_url, sim, stripe):
    app = site(fms_url)
    app.state.front_desk.processor = StripeProcessor('sk_test_fake', base_url=stripe.url, clock=lambda: NOW.timestamp())
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        units = [u for u in vacant(fd, ident, 20) if rent_of(fd, ident).units[u].rent_cents]
        path = begin(c, units[0]).headers['location']
        page = c.get(path).text
        form = {"token": token(page), "accept": "yes", "card": "card_declined", "sha256": agreement_hash(page)}
        c.post(path, data=form, follow_redirects=False)
        c.post(path, data=form, follow_redirects=False)
        (rental,) = own(fd, ident)
        assert len(rental.pay_ops) == 1
        again = c.get(path).text
        assert 'you accepted this agreement' in seen(again).lower() and 'name="accept"' not in again
        c.post(path, data={"token": token(again), "card": "card_ok"}, follow_redirects=False)
        (rental,) = own(fd, ident)
        assert rental.state == 'active' and len(rental.pay_ops) == 2


def test_typed_details_never_pick_an_existing_tenant_or_spend_their_credit(fms_url, sim, stripe):
    app = site(fms_url)
    app.state.front_desk.processor = StripeProcessor('sk_test_fake', base_url=stripe.url, clock=lambda: NOW.timestamp())
    with sign_in(app) as c, sign_in(app) as other:
        fd, ident = desk_of(app, c)
        _, other_id = desk_of(app, other)
        first = fd.add_renter(ident, 'Rae Quinn', '+14075550199')
        assert fd.add_renter(ident, 'Rae Quinn', '+14075550199') != first
        assert first not in fd.view(other_id)[0].tenants
        units = [u for u in vacant(fd, ident, 20) if rent_of(fd, ident).units[u].rent_cents]
        known = fd.view(ident)[0].tenants[first]
        with fd.lock:                   # the known tenant holds more credit than any move-in costs
            store, stream = fd.ready(), fd.stream(ident)
            with store.write():
                led = Ledger(store.books(stream))
                led.receive_payment(first, 100000, NOW)
                store.append(stream, led.pending)
        path = begin(c, units[0], name=known.name, phone=known.phone).headers['location']
        (rental,) = own(fd, ident)
        assert rental.tenant != first
        finish(c, path, 'card_declined')
        assert rent_of(fd, ident).rentals[rental.id].state != 'active'


def test_the_size_links_show_only_units_of_that_size(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        units = rent_of(fd, ident).units
        offered = lambda html: set(re.findall(r'href="/rent/([^"]+)"', html))
        everything = offered(c.get('/rent').text)
        sizes = {units[u].size for u in everything}
        assert len(sizes) > 1
        for size in sizes:
            page = c.get('/rent', params={'size': size}).text
            assert offered(page) == {u for u in everything if units[u].size == size}
            assert re.search(r'href="/rent\?size=[^"]*" aria-current="page">' + re.escape(size) + '<', page)
        assert offered(c.get('/rent', params={'size': 'no such size'}).text) == everything


def test_website_needs_sign_in(fms_url, sim):
    with TestClient(site(fms_url)) as c:
        r = c.get('/rent', follow_redirects=False)
        assert r.status_code == 303 and r.headers['location'] == '/sign-in'


def test_code_not_read_back_is_not_shown_as_confirmed(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        fd.access = renting.StandIn(hide={renting.ACCESS_LEVEL})
        unit = vacant(fd, ident)[0]
        address = begin(c, unit).headers['location']
        result, _ = finish(c, address)
        text = seen(c.get(result.headers['location']).text)
        assert 'Active' in text and 'code could not be read back' in text
        assert 'Code set and read back' not in text


def test_agreement_shows_exact_core_text_and_can_be_accepted(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unit = vacant(fd, ident)[0]
        address = begin(c, unit, name='FAILED & <Rae>').headers['location']
        page = c.get(address).text
        rental = next(iter(own(fd, ident)))
        agreement = rent_of(fd, ident).agreements[rental.agreement]
        rendered = re.search(r'<div class="website-document">(.*?)</div>', page, re.S).group(1)
        assert unescape(rendered).encode('utf-8') == agreement.text.encode('utf-8')
        assert '&lt;Rae&gt;' in rendered
        assert f'Version {agreement.version}' in page
        result, _ = finish(c, address)
        assert result.status_code == 303
        assert rent_of(fd, ident).rentals[rental.id].accepted


def test_website_writes_require_a_form_token(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c:
        fd, ident = desk_of(app, c)
        unit = vacant(fd, ident)[0]
        tenants = len(fd.view(ident)[0].tenants)
        response = c.post(f'/rent/{unit}', data={'name': 'Rae', 'phone': '555'}, follow_redirects=False)
        assert response.status_code == 422
        assert len(fd.view(ident)[0].tenants) == tenants
        assert not own(fd, ident)
        address = begin(c, unit).headers['location']
        page = c.get(address).text
        response = c.post(address, data={'accept': 'yes', 'card': 'card_ok', 'sha256': agreement_hash(page)}, follow_redirects=False)
        assert response.status_code == 422
        rental = next(iter(own(fd, ident)))
        assert not rental.accepted and not rental.pay_ops
