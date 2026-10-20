import json
import logging
import os
import random
from typing import Any

import asyncpg
from arq import Retry
from arq.connections import RedisSettings
from dotenv import load_dotenv
from httpx import AsyncClient

from .exceptions import GithubPermError, GithubRetryError
from .github_client import DiffFile, GithubClient, PullRequestMeta

load_dotenv()
REDIS_SETTINGS = RedisSettings.from_dsn(os.environ["REDIS_URL"])
#configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s  - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    ctx['http_client'] = AsyncClient()
    ctx['pool'] = await asyncpg.create_pool(dsn=os.environ["DATABASE_URL"], command_timeout = 10)
    ctx['ghclient'] = GithubClient()

async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx['http_client'].aclose()
    await ctx['pool'].close()

def compute_delay(job_try: int) -> int:
    delay = 2 * pow(2,(job_try - 1))
    delay = min(delay, 32)
    jitter = random.randint(0, delay)
    return jitter+int(delay)

async def fetch_diff(ctx: dict[str, Any], delivery_id: str, request_id: str) -> None:
    #read event row using delivery_id, get repo and pr_number
    query1 = "SELECT id, repo, pr_number FROM webhook_events WHERE delivery_id = $1;"
    events_params = [delivery_id]
    psql_response_events = await ctx['pool'].fetchrow(query1, *events_params)#use fetchrow because need to index with a string. fetch() checks against a list, and indexing list with str gives typeerror()
    if psql_response_events is None:
        logger.warning(json.dumps({"request_id": request_id ,"message": f"Webhook event with delivery_id = {delivery_id} not found."}))
        return
    #Split repo. It is stored as owner/name. Your client takes them separately.
    event_id = psql_response_events["id"]
    repo = psql_response_events["repo"].split("/")[1]
    owner = psql_response_events["repo"].split("/")[0]
    pr_number = psql_response_events["pr_number"]
    try:
        pr_metadata: PullRequestMeta = await ctx["ghclient"].get_pull_request(client = ctx["http_client"], owner=owner, repo = repo, number = pr_number)
    except GithubRetryError as e:
        if e.retry_after:
            logger.warning(json.dumps({"request_id": request_id ,"message": f"Retry #{ctx['job_try']} pr metadata pull with Retry_after: {e.retry_after}."}))
            raise Retry(defer=e.retry_after) from e
        defer=compute_delay(ctx["job_try"])
        logger.warning(json.dumps({"request_id": request_id ,"message": f"Retry #{ctx['job_try']} pr metadata pull with computed delay: {defer}."}))
        raise Retry(defer) from e
    except GithubPermError as e:
        logger.error(json.dumps({"request_id": request_id ,"message": f"Non-retryable error for pr metadata pull: {e}."}))
        return
    logger.info(json.dumps({"request_id": request_id ,"message": f"PR Metadata received for repo: {repo}, pr: {pr_number}."}))

    head = pr_metadata.head.sha
    base = pr_metadata.base.sha

    #idempotency check
    query2 = """SELECT 1 FROM diffs WHERE
    repo = $1
    AND pr_number = $2
    AND head_sha = $3
    AND base_sha = $4
    LIMIT 1;
    """
    diff_params = [psql_response_events["repo"], pr_number, head, base]
    psql_response_diffs = await ctx['pool'].fetch(query2, *diff_params)
    if psql_response_diffs != []:
        logger.warning(json.dumps({"request_id": request_id ,"message": f"Diff already fetched for repo: {repo}, pr: {pr_number}."}))
        return
    try:
        pr_files_tuple: tuple[list[DiffFile], bool] = await ctx["ghclient"].list_pull_request_files(client = ctx["http_client"], owner=owner, repo = repo, number = pr_number)
    except GithubRetryError as e:
        if e.retry_after:
            logger.warning(json.dumps({"request_id": request_id ,"message": f"Retry #{ctx['job_try']} pr file pull with Retry_after: {e.retry_after}."}))
            raise Retry(defer=e.retry_after) from e
        defer=compute_delay(ctx["job_try"])
        logger.warning(json.dumps({"request_id": request_id ,"message": f"Retry #{ctx['job_try']} pr file pull with computed delay: {defer}."}))
        raise Retry(defer) from e
    except GithubPermError as e:
        logger.error(json.dumps({"request_id": request_id ,"message": f"Non-retryable error for pr file pull: {e}."}))
        return
    pr_files = pr_files_tuple[0]
    incomplete_flag = pr_files_tuple[1]
    logger.info(json.dumps({"request_id": request_id ,"message": f"PR Files received for repo: {repo}, pr: {pr_number}. File count: {len(pr_files)}"}))
    if incomplete_flag:
        logger.warning(json.dumps({"request_id": request_id ,"message": "Diff files doesnt include all the files, as it exceeded the maximum amount."}))

    #inserts happen after both github calls so no half-written diff is possible
    await insert_diff(pool=ctx["pool"], request_id=request_id, event_id=event_id, repo=psql_response_events["repo"], pr_number=pr_number, base_sha=base, head_sha=head, pr_files=pr_files )


async def insert_diff(pool: asyncpg.Pool, request_id: str, event_id: int, repo: str, pr_number: int, base_sha: str, head_sha: str, pr_files: list[DiffFile]) -> None:
    diffs_query = "INSERT INTO diffs (event_id, repo, pr_number, base_sha, head_sha) VALUES ($1, $2, $3, $4, $5) ON CONFLICT (repo, pr_number, head_sha, base_sha) DO NOTHING RETURNING id;"
    diff_files_query = "INSERT INTO diff_files (diffref_id, filename, status, additions, deletions, changes, patch) VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (diffref_id, filename) DO NOTHING"

    async with pool.acquire() as conn, conn.transaction():
        diff_id = await conn.fetchval(diffs_query, event_id, repo, pr_number, base_sha, head_sha)
        if diff_id is None:
            logger.warning(json.dumps({"request_id": request_id ,"message": "Conflict detected for diff insert. Insert was already done by another worker."}))
            return
        diff_files_list = []
        for f in pr_files:
            diff_files_list.append((diff_id, f.filename, f.status, f.additions, f.deletions, f.changes, f.patch))
        await conn.executemany(diff_files_query, diff_files_list)
        logger.info(json.dumps({"request_id": request_id ,"message": f"Diffs and Diff_files insert completed for repo: {repo}, pr: {pr_number}."}))
        return

class WorkerSettings:
    functions = (fetch_diff,)
    redis_settings = REDIS_SETTINGS
    on_startup = startup
    on_shutdown = shutdown
    max_tries = 5

