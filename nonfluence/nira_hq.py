"""Read project and issue records using a dedicated read-only Nira HQ token."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from .references import nira_hq_url
from .store import Invalid, Missing


class DenyRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ReadOnlyNiraHQ:
    def __init__(self, credential_path, base_url):
        self.base_url = nira_hq_url(base_url)
        if not self.base_url:
            raise ValueError("Nira HQ resolution requires a configured base URL.")
        token = Path(credential_path).read_text().strip()
        if not re.fullmatch(r'nira_[A-Za-z0-9_-]{20,160}', token):
            raise ValueError('Invalid Nira HQ read credential.')
        self._opener = build_opener(DenyRedirects())
        self._headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}

    def _record(self, kind, reference):
        try:
            ref = str(UUID(reference))
        except ValueError:
            ref = reference.upper()
            pattern = r'[A-Z][A-Z0-9_]{1,15}' + (r'-[1-9][0-9]{0,17}' if kind == 'issues' else '')
            if not re.fullmatch(pattern, ref):
                raise Invalid('Use a Nira key or stable UUID.') from None
        request = Request(f'{self.base_url}/api/{kind}/{quote(ref, safe="")}', headers=self._headers)
        try:
            with self._opener.open(request, timeout=3) as response:
                record = json.load(response)
        except HTTPError as error:
            if error.code == 404:
                raise Missing('Nira record not found.') from None
            raise ConnectionError('Nira HQ returned an API error.') from None
        except (URLError, TimeoutError, ValueError) as error:
            raise ConnectionError('Nira HQ is unavailable.') from None
        if not isinstance(record, dict) or not record.get('id'):
            raise ConnectionError('Nira HQ returned an invalid record.')
        return record

    def project(self, reference):
        return self._record('projects', reference)

    def issue(self, reference):
        return self._record('issues', reference)

    def resolve_many(self, references):
        def resolve(reference):
            kind, target = reference
            try:
                record = self.project(target) if kind == 'nira_hq_project' else self.issue(target)
            except Missing:
                return {'state': 'broken'}
            except ConnectionError:
                return {'state': 'unavailable'}
            return {'state': 'resolved', 'stable_id': record['id'], 'display_key': record['key'],
                    'title': (record.get('name') or record['title']), 'type': record.get('type', 'project'),
                    'status': record['status'].replace('_', ' ').capitalize()}

        batch = list(dict.fromkeys(references))[:16]
        with ThreadPoolExecutor(max_workers=8) as pool:
            return dict(zip(batch, pool.map(resolve, batch)))
