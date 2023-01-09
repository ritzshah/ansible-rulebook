import asyncio
import json
import logging
import os
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urljoin, urlparse

import aiohttp
import dpath

from ansible_rulebook.exception import ControllerApiException

logger = logging.getLogger(__name__)


# https://docs.ansible.com/ansible-tower/latest/html/towerapi/access_resources.html#access-resources
URL_PATH_RESERVED_CHARSET = {}
for c in ";/?:@=&[]":
    URL_PATH_RESERVED_CHARSET[c] = quote(c, safe="")
URL_PATH_RESERVED_CHARSET["+"] = "[+]"


def _encode_uri(text: str) -> str:
    for c in URL_PATH_RESERVED_CHARSET:
        if c in text:
            text = text.replace(c, URL_PATH_RESERVED_CHARSET[c])
    return text


class JobTemplateRunner:
    JOB_TEMPLATE_SLUG = "/api/v2/job_templates"
    VALID_POST_CODES = [200, 201, 202]
    JOB_COMPLETION_STATUSES = ["successful", "failed", "error", "canceled"]

    def __init__(self, host: str, token: str):
        self.token = token
        self.host = host
        self.refresh_delay = int(
            os.environ.get("EDA_JOB_TEMPLATE_REFRESH_DELAY", 10)
        )

    async def _get_page(
        self, session: aiohttp.ClientSession, href_slug: str, params: dict
    ) -> dict:
        url = urljoin(self.host, href_slug)
        async with session.get(url, params=params) as response:
            response_text = dict(
                status=response.status, body=await response.text()
            )
        if response_text["status"] != 200:
            raise ControllerApiException(
                "Failed to get from %s. Status: %s, Body: %s"
                % (
                    url,
                    response_text["status"],
                    response_text.get("body", "empty"),
                )
            )
        return response_text

    def _auth_headers(self) -> dict:
        return dict(Authorization=f"Bearer {self.token}")

    async def run_job_template(
        self,
        name: str,
        organization: str,
        job_params: dict,
        event_handler: Callable[[dict], Any],
    ) -> str:
        job = await self.launch(name, organization, job_params)

        url_info = urlparse(job["url"])
        url = f"{url_info.path}/job_events/"
        counters = []
        params = dict(parse_qsl(url_info.query))

        async with aiohttp.ClientSession(
            headers=self._auth_headers()
        ) as session:
            while True:
                response = await self._get_page(session, url, params)
                json_body = json.loads(response["body"])
                job_status = None
                for event in json_body["results"]:
                    job_status = dpath.get(
                        event, "summary_fields.job.status", "."
                    )
                    counter = event["counter"]
                    if counter not in counters:
                        counters.append(counter)
                        print(event["stdout"])
                    if event_handler:
                        await event_handler(event)

                if json_body.get("next", None):
                    params["page"] = params.get("page", 1) + 1
                    continue

                if job_status in self.JOB_COMPLETION_STATUSES:
                    return job_status
                await asyncio.sleep(self.refresh_delay)

    async def launch(
        self, name: str, organization: str, job_params: dict
    ) -> dict:
        name_uri = _encode_uri(name)
        org_uri = _encode_uri(organization)
        resource_uri = f"{name_uri}++{org_uri}"
        url = urljoin(
            self.host, f"{self.JOB_TEMPLATE_SLUG}/{resource_uri}/launch/"
        )

        async with aiohttp.ClientSession(
            headers=self._auth_headers()
        ) as session:
            async with session.post(url, json=job_params) as post_response:
                response = dict(
                    status=post_response.status,
                    body=await post_response.text(),
                )

                if response["status"] not in self.VALID_POST_CODES:
                    raise ControllerApiException(
                        "Failed to post to %s. Status: %s, Body: %s"
                        % (
                            url,
                            response["status"],
                            response.get("body", "empty"),
                        )
                    )
                json_body = json.loads(response["body"])
        return json_body


# For testing only. To be removed after intergration with rulebook action
if __name__ == "__main__":
    import pprint
    from datetime import datetime

    runner = JobTemplateRunner("<controller host>", "<api token>")

    event_log = asyncio.Queue()
    time_now = str(datetime.utcnow())

    async def event_callback(event: dict) -> None:
        await event_log.put(
            {
                "type": "AnsibleEvent",
                "event": {
                    "uuid": event["uuid"],
                    "counter": event["counter"],
                    "stdout": event["stdout"],
                    "start_line": event["start_line"],
                    "end_line": event["end_line"],
                    "runner_ident": "<UUID N/A>",
                    "event": event["event"],
                    "pid": "<N/A>",
                    "created": event["created"],
                    "parent_uuid": event["parent_uuid"],
                    "event_data": event["event_data"],
                    "job_id": "uuid",
                    "ansible_rulebook_id": "id",
                },
                "run_at": time_now,
            }
        )

    ret = asyncio.run(
        runner.run_job_template(
            "Demo Job Template", "Default", {}, event_handler=event_callback
        )
    )
    print("Job template running finished with status", ret)
    while not event_log.empty():
        log = event_log.get_nowait()
        pprint.pprint(log)