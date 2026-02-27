# ruff: noqa: E402

import datetime as dt
import logging
import sys
import time
from typing import Dict, List

import dateparser

# Disable loggers prior to package imports
import processor

processor.disable_package_loggers()

import mcmetadata.urls as urls
from newsdataapi import NewsDataApiClient

import processor.database as database
import processor.database.projects_db as projects_db
import processor.database.stories_db as stories_db
import processor.projects as projects
import processor.tasks.classification as classification_tasks
import scripts.tasks as tasks
from processor import NEWSDATA_API_KEY
from processor.classifiers import download_models

MAX_QUERY_LENGTH = 512

DAY_OFFSET = 0  # they claim to have pseudo-relatime data
DAY_WINDOW = (
    2  # our max stories is small here, so don't look for stories that are too old
)

# We have 20,000 credits each month (645 per day). So at 5 credits per API hit that's ~130 hits / day,
# With 85 projects (below 512 character limit), we have to avg < 2 hits / project.
PAGE_SIZE = 50  # per API spec that is max
AVG_HITS_PER_PROJECT = 1  # we want to aim for this many hits per project
MAX_STORIES_PER_PROJECT = (
    PAGE_SIZE * AVG_HITS_PER_PROJECT
)  # try to hit avg, because we'll only get this many on a few projects

# Rate limit is  1800 credits every 15 minute, which is 90,000 articles / 15 minutes. That's more than we can
# fetch each day given our account level, so we can set a low rate limit here
MAX_CALLS_PER_SEC = 1  # throttle calls to NewsData to avoid rate limiting
DELAY_SECS = 1 / MAX_CALLS_PER_SEC  # may need to be further adjusted

# initialize api client, maybe do this in processor initialization?
newsdata_api = NewsDataApiClient(apikey=NEWSDATA_API_KEY)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def load_projects_task() -> List[Dict]:
    project_list = projects.load_project_list(
        force_reload=True, overwrite_last_story=False
    )
    logger.info("  Checking {} projects".format(len(project_list)))
    # return [p for p in project_list if p['id'] == 166]
    return project_list


def _project_story_worker(args: Dict) -> Dict:
    p = args
    Session = database.get_session_maker()
    # build a time frame to search in
    start_date, end_date = projects.query_start_end_dates(
        p,
        Session,
        DAY_OFFSET,
        DAY_WINDOW,
        processor.SOURCE_NEWSDATA,
    )
    # Newsdata.io takes strings as parameters and not datetime objects
    from_date = start_date.strftime("%Y-%m-%d")
    to_date = end_date.strftime("%Y-%m-%d")

    project_email_message = ""
    logger.info("Checking project {}/{}".format(p["id"], p["title"]))
    logger.debug(
        "  {} stories/page up to {}".format(PAGE_SIZE, MAX_STORIES_PER_PROJECT)
    )
    project_email_message += "Project {} - {}:\n".format(p["id"], p["title"])

    terms_no_curlies = p["search_terms"].replace("“", '"').replace("”", '"')

    # see how many stories and fetch them page by page
    story_count = 0
    page_count = 0
    more_stories = True
    page_token = None
    latest_pub_date = dt.datetime.now() - dt.timedelta(weeks=50)  # a while ago
    while more_stories and (story_count < MAX_STORIES_PER_PROJECT):
        try:
            # fetch stories and return results
            response = newsdata_api.archive_api(
                q=terms_no_curlies,  # query limit may have changed (listed as <=512 characters in updated documentation)
                language=p["language"].lower(),
                from_date=from_date,
                to_date=to_date,
                full_content=True,  # make sure to collect full text
                country=p["newscatcher_country"],
                size=PAGE_SIZE,
                page=page_token,
                # docs say sort is by "publish date (newest first)" if no sort specified (that's what we want)
            )
            page_of_stories = response["results"]
            total_stories = response["totalResults"]
            page_token = response["nextPage"]
            logger.info("  Project {}: {} total stories".format(p["id"], total_stories))
            if page_of_stories:
                logger.info(
                    "    {} - page {}: ({}) stories".format(
                        p["id"], page_count, len(page_of_stories)
                    )
                )
                skipped_dupes = 0  # how many URLs do we filter out because they're already in the DB for this project recently
                already_processed_normalized_urls = set()
                if total_stories > 0:
                    # list recent urls to filter so we don't fetch text extra if we've recently proceses already
                    # (and will be filtered out by add_stories call in later paost-text-fetch step)
                    with Session() as db_session:
                        already_processed_normalized_urls = (
                            stories_db.project_story_normalized_urls(db_session, p, 14)
                        )
                page_latest_pub_date = [
                    dateparser.parse(s["pubDate"]) for s in page_of_stories
                ]
                latest_pub_date = max(latest_pub_date, max(page_latest_pub_date))
                cleaned_page_of_stories = (
                    []
                )  # make sure we respect max stories per project by using this from now on
                for s in page_of_stories:
                    if story_count >= MAX_STORIES_PER_PROJECT:
                        break
                    real_url = s["link"]
                    # skip URLs we've processed recently
                    if (
                        urls.normalize_url(real_url)
                        in already_processed_normalized_urls
                    ):
                        skipped_dupes += 1
                        continue
                    s["source"] = processor.SOURCE_NEWSDATA
                    s["publish_date"] = str(s["pubDate"])
                    s["project_id"] = p["id"]
                    s["authors"] = ", ".join(s["creator"]) if s.get("creator") else None
                    s["story_text"] = s["content"]
                    s["url"] = real_url
                    s["media_name"] = s["source_name"]
                    s["media_id"] = s["source_id"]
                    s["media_url"] = s["source_url"]
                    cleaned_page_of_stories.append(s)
                page_count += 1
                # and log that we got and queued them all
                with Session() as session:
                    stories_to_queue = stories_db.add_stories(
                        session, cleaned_page_of_stories, p, processor.SOURCE_NEWSDATA
                    )
                    story_count += len(stories_to_queue)
                    classification_tasks.classify_and_post_worker.delay(
                        p, stories_to_queue
                    )
                    # important to write this update now, because we have queued up the task to process these stories
                    # the task queue will manage retrying with the stories if it fails with this batch
                    projects_db.update_history(
                        session,
                        p["id"],
                        latest_pub_date,  # this will be interpreted next time as GMT, so make sure it is(!)
                        processor.SOURCE_NEWSDATA,
                    )
                    more_stories = page_token is not None
                time.sleep(DELAY_SECS)
                if len(page_of_stories) < PAGE_SIZE:
                    # got back less than a full page, no more stories to fetch (don't waste an API hit)
                    more_stories = False
            else:
                more_stories = False
        except Exception as e:
            logger.exception(
                "  Couldn't count/retrieve stories in project {}. Skipping project for now. {}".format(
                    p["id"], e
                )
            )
            project_email_message += (
                "    failed to count and/or retrieve stories with {}\n\n".format(e)
            )
            return dict(
                email_text=project_email_message,
                stories=story_count,
                pages=page_count,
            )

    logger.info(
        "  queued {} stories for project {}/{} (in {} pages)".format(
            story_count, p["id"], p["title"], page_count
        )
    )
    #  add a summary to the email we are generating
    warnings = ""
    if story_count > (
        MAX_STORIES_PER_PROJECT * 0.8
    ):  # try to get our attention in the email
        warnings += "(⚠️️️ query might be too broad)"
    project_email_message += "    found {} new stories (over {} pages) {}\n\n".format(
        story_count, page_count, warnings
    )
    return dict(
        email_text=project_email_message,
        stories=story_count,
        pages=page_count,
    )


def process_projects(project_list: List[Dict]) -> List[Dict]:
    results = []
    for p in project_list:
        results.append(_project_story_worker(p))
    return results


if __name__ == "__main__":
    logger.info("Starting {} story fetch job".format(processor.SOURCE_NEWSDATA))

    # important to do because there might be new models on the server!
    logger.info("  Checking for any new models we need")
    models_downloaded = download_models()
    logger.info(f"    models downloaded: {models_downloaded}")
    if not models_downloaded:
        sys.exit(1)

    start_time = time.time()

    # 1. list all the project we need to work on
    all_projects_list = load_projects_task()
    projects_list = [
        p for p in all_projects_list if len(p["search_terms"]) < MAX_QUERY_LENGTH
    ]
    logger.info(f"  {len(projects_list)}/{len(all_projects_list)} projects to process")

    # 2. process all the projects and queue results by project
    logger.info("Processing project")
    project_results = process_projects(projects_list)
    logger.info(f"Total stories queued: {sum([p['stories'] for p in project_results])}")

    # 3. send email/slack_msg with results of operations
    tasks.send_project_list_slack_message(
        project_results, processor.SOURCE_NEWSDATA, start_time
    )
    tasks.send_project_list_email(
        project_results, processor.SOURCE_NEWSDATA, start_time
    )
