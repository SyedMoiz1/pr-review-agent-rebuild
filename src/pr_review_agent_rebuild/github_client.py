#Github client
import asyncio
import os
from json.decoder import JSONDecodeError
from typing import NoReturn

import httpx
from dotenv import load_dotenv
from httpx import AsyncClient, Headers
from pydantic import BaseModel

from .exceptions import GithubPermError, GithubRetryError, MalformedPayload

load_dotenv()
gh_token = os.environ["GITHUB_TOKEN"]

class Sha(BaseModel):
    sha: str
    ref: str

class PullRequestMeta(BaseModel):
    head: Sha
    base: Sha
    number: int
class DiffFile(BaseModel):
    filename: str
    status: str
    changes: int
    additions: int
    deletions: int
    patch: str | None

#need a github client class
#This class will have 2 functions: 
#get_pull_request(client, owner, repo, number) -> PullRequestMeta
#list_pull_request_files(client, owner, repo, number) -> list[DiffFile]
    #Paginated. Set per_page=100. Follow the Link header until there is no rel="next".
    #httpx parses that header for you: response.links gives a dict, and response.links.get("next", {}).get("url") is the next page. Do not write a Link header parser.
    #Cap page count, decide what happens when limit hits

class GithubClient:
    def __init__(self) -> None:
        self.headers ={"Authorization": f"Bearer {gh_token}","Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        self.base_url = 'https://api.github.com'

    def app_error_handler(self, status: int, headers: Headers | None = None) -> NoReturn:
        if status == 404:
            raise GithubPermError(reason = "404 Not Found")
        elif status == 408:
            raise GithubRetryError(reason = "408 Request Timed out. Retry recommended.")
        elif status == 403:
            if headers and "Retry-After" in headers:
                raise GithubRetryError(reason = "403 Forbidden. Retry recommended.", retry_after=float(headers["Retry-After"]))
            raise GithubPermError(reason="403 Forbidden")
        elif status == 401:
            raise GithubPermError(reason = "401 Unauthorized")
        elif status == 429:
            if headers and "Retry-After" in headers:
                raise GithubRetryError(reason = "429 Too Many Requests. Retry recommended.", retry_after=float(headers["Retry-After"]))
            raise GithubRetryError(reason = "429 Too Many Requests. Retry recommended.")
        elif 500 <= status < 600:
            raise GithubRetryError(reason = "Internal Server Error. Retry recommended.")
        else: raise GithubPermError(reason="Unrecognized Error")

    async def get_pull_request(self, client: AsyncClient, owner: str, repo: str, number: int) -> PullRequestMeta:
        async with asyncio.timeout(30): #total deadline for task
            try:
                try:
                    async with asyncio.timeout(5):
                        response = await client.get(url=self.base_url+f"/repos/{owner}/{repo}/pulls/{number}", headers=self.headers)
                except (httpx.TimeoutException, httpx.ConnectError): #exception is handled inside
                    self.app_error_handler(status=408)
                
                status = response.status_code
                if 400 <= status < 600:
                    self.app_error_handler(status=status, headers=response.headers)
                model = PullRequestMeta(**response.json())
                return model
            except JSONDecodeError as e:
                raise MalformedPayload(reason = "Invalid Data Fields.") from e



    async def list_pull_request_files(self, client: AsyncClient, owner: str, repo: str, number: int) -> tuple[list[DiffFile], bool]:
        async with asyncio.timeout(30): #total deadline for task
            try:
                diffs = []
                page_limit = 20
                per_page = 100
                url: str | None = self.base_url+f"/repos/{owner}/{repo}/pulls/{number}/files?per_page={per_page}"
                page = 1
                incomplete = False
                while url is not None:
                    if page >page_limit:
                        incomplete = True
                        break
                    try:
                        async with asyncio.timeout(5):
                            response = await client.get(url=url, headers=self.headers)
                    except (httpx.TimeoutException, httpx.ConnectError): #exception is handled inside
                        self.app_error_handler(status=408)

                    status = response.status_code
                    if 400 <= status < 600:
                        self.app_error_handler(status=status, headers=response.headers)

                    for file in response.json():
                        diffs.append(DiffFile(**file))
                    if response.links.get("next"):
                        url = response.links.get("next", {}).get("url")
                        page += 1
                        continue
                    break
                
                return (diffs, incomplete)
            except JSONDecodeError as e:
                raise MalformedPayload(reason = "Invalid Data Fields.") from e


async def test_client() -> tuple[PullRequestMeta, tuple[list[DiffFile], bool]]:
    async with httpx.AsyncClient() as client:
        gh = GithubClient()
        pr_meta: PullRequestMeta = await gh.get_pull_request(client=client, owner="SyedMoiz1", repo="pr-review-agent-rebuild", number=8)
        pr_files: tuple[list[DiffFile], bool] = await gh.list_pull_request_files(client=client, owner="SyedMoiz1", repo="pr-review-agent-rebuild", number=8)
        return (pr_meta, pr_files)

# tup = asyncio.run(test())
# print(tup)