"""Lien pages show one copy of the books and send staff steps through the gate."""
import re

from fastapi.testclient import TestClient

from tests.test_fms import record_of, seen, sign_in, site
from tests.test_fms_collections import token


def step(client, url, page, **fields):
    return client.post(url + "/lien", data={"token": token(page), **fields}, follow_redirects=False)


def test_seeded_file_and_list(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        page = client.get('/liens').text
        assert 'Tom Becker' in seen(page)
        assert '#lien' in page
        url = record_of(client, 'Tom Becker')
        text = seen(client.get(url).text)
        assert 'Lien file' in text and 'Review' in text
        assert 'delivery of the notice' in text.lower() and 'newspaper advertisement' in text.lower()
        assert '23 tenants owe $2,996.00' in seen(client.get('/collections').text)


def test_stop_and_refusal_are_kept_on_one_copy(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as client, sign_in(app) as other:
        url = record_of(client, 'Tom Becker')
        page = client.get(url).text
        section = page.split('id="lien"', 1)[1]
        lease = re.search(r'name="lease" value="([A-Z0-9]+)"', section).group(1)
        case = re.search(r'name="case" value="([A-Z0-9]+)"', section).group(1)
        step(client, url, page, action='record_lien_stop', lease=lease, case=case,
             kind='court_order', detail='Court order reported', source='Court letter')
        assert 'Court order reported' in seen(client.get(url).text)
        assert 'Court order reported' not in seen(other.get(url).text)
        page = client.get(url).text
        step(client, url, page, action='prepare_lien_notice', lease=lease, case=case)
        assert 'Nothing changed.' in seen(client.get(url + '?at=lien').text)
        assert 'Nothing changed.' not in seen(client.get(url).text)
        assert 'A manager approves' in seen(page)
        assert 'value="approve_lien_case"' not in page


def test_lien_pages_require_sign_in(fms_url, sim):
    with TestClient(site(fms_url)) as client:
        assert client.get('/liens', follow_redirects=False).headers['location'] == '/sign-in'
        assert client.post('/tenants/unknown/lien', follow_redirects=False).headers['location'] == '/sign-in'


def test_address_and_notice_copy_go_through_the_gate(fms_url, sim):
    with sign_in(site(fms_url)) as client:
        url = record_of(client, 'Tom Becker')
        page = client.get(url).text
        section = page.split('id="lien"', 1)[1]
        lease = re.search(r'name="lease" value="([A-Z0-9]+)"', section).group(1)
        case = re.search(r'name="case" value="([A-Z0-9]+)"', section).group(1)
        step(client, url, page, action='record_lien_address', lease=lease, case=case,
             address='123 Main Street, Orlando, FL', basis='written change of address')
        page = client.get(url).text
        step(client, url, page, action='prepare_lien_notice', lease=lease, case=case)
        page = client.get(url).text
        assert 'Evidence incomplete' in seen(page)
        assert 'Frozen ledger copy:' in seen(page)
        step(client, url, page, action='list_lien_auction', lease=lease, case=case)
        assert 'Nothing changed.' in seen(client.get(url + '?at=lien').text)
