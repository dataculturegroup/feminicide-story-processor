import logging
from typing import List, Dict
from prefect import Flow, Parameter, task, unmapped
from prefect.executors import LocalDaskExecutor
import datetime as dt
from mcmetadata import extract
import requests
import sys
import time
from waybacknews.searchapi import SearchApiClient
import processor.database.stories_db as stories_db
from processor.classifiers import download_models
from processor import SOURCE_WAYBACK_MACHINE, SOURCE_NEWSCATCHER
import processor.projects as projects
from processor.tasks import add_entities_to_stories
import processor.util as util
import scripts.tasks as prefect_tasks

DEFAULT_STORIES_PER_PAGE = 100  # I found this performs poorly if set higher than 100
WORKER_COUNT = 8
DATE_LIMIT = 30


@task(name='load_projects')
def load_projects_task() -> List[Dict]:
    project_list = projects.load_project_list(force_reload=True, overwrite_last_story=False)
    logger.info("  Checking {} projects".format(len(project_list)))
    return project_list


@task(name='process_project')
def process_project_task(project: Dict, page_size: int) -> Dict:
    project_email_message = ""
    project_email_message += "Project {} - {}:\n".format(project['id'], project['title'])
    needing_posting_count = stories_db.unposted_above_story_count(project['id'], DATE_LIMIT)
    logger.info("Project {} - {} unposted above threshold stories to process".format(
        project['id'], needing_posting_count))
    story_count = 0
    page_count = 0
    wm_api = SearchApiClient("mediacloud")
    if needing_posting_count > 0:
        db_stories = stories_db.unposted_stories(project['id'], DATE_LIMIT)
        for db_stories_page in util.chunks(db_stories, page_size):
            # find the matching story from the source
            source_stories = []
            for s in db_stories_page:
                try:
                    if s['source'] == SOURCE_WAYBACK_MACHINE:
                        url_for_query = s['url'].replace("/", "\\/").replace(":", "\\:")
                        matching_stories = wm_api.sample(f"url:{url_for_query}", dt.datetime(2010, 1, 1),
                                                         dt.datetime(2030, 1, 1))
                        matching_story = requests.get(matching_stories[0]['article_url']).json()  # fetch the content (in `snippet`)
                        matching_story['stories_id'] = s['id']
                        matching_story['source'] = s['source']
                        matching_story['media_url'] = matching_story['domain']
                        matching_story['media_name'] = matching_story['domain']
                        matching_story['publish_date'] = matching_story['publication_date']
                        matching_story['log_db_id'] = s['id']
                        matching_story['project_id'] =s['project_id']
                        matching_story['language_model_id'] = project['language_model_id']
                        matching_story['story_text'] = matching_story['snippet']
                        source_stories += [matching_story]
                    elif s['source'] == SOURCE_NEWSCATCHER:
                        metadata = extract(url=s['url'])
                        story = dict(
                            stories_id=s['stories_id'],
                            source=s['source'],
                            language=metadata['language'],
                            media_url=metadata['canonical_domain'],
                            media_name=metadata['canonical_domain'],
                            publish_date=str(metadata['publication_date']),
                            title=metadata['article_title'],
                            url=metadata['url'],  # resolved
                            log_db_id=s['stories_id'],
                            project_id=s['project_id'],
                            language_model_id=project['language_model_id'],
                            story_text=metadata['text_content']
                        )
                        source_stories += [story]
                except Exception as e:
                    logger.warning(f"Skipping {s['url']} due to {e}")
            # add in entities
            source_stories = add_entities_to_stories(source_stories)
            # add in the scores from the logging db
            db_story_2_score = {r['stories_id']: r for r in db_stories_page}
            for s in source_stories:
                s['confidence'] = db_story_2_score[s['stories_id']]['model_score']
                s['model_1_score'] = db_story_2_score[s['stories_id']]['model_1_score']
                s['model_2_score'] = db_story_2_score[s['stories_id']]['model_2_score']
            # strip unneeded fields
            stories_to_send = projects.prep_stories_for_posting(project, source_stories)
            # send to main server
            projects.post_results(project, stories_to_send)
            # and log that we did it
            stories_db.update_stories_posted_date(stories_to_send)
            story_count += len(stories_to_send)
            logger.info("    sent page of {} stories for project {}".format(len(stories_to_send), project['id']))
            page_count += 1
    logger.info("  sent {} stories for project {} total (of {})".format(story_count, project['id'], needing_posting_count))
    #  add a summary to the email we are generating
    project_email_message += "    posted {} stories from db ({} pages)\n\n".format(story_count, page_count)
    return dict(
        email_text=project_email_message,
        stories=story_count,
        pages=page_count,
    )


if __name__ == '__main__':

    logger = logging.getLogger(__name__)
    logger.info("Starting story catchup job")

    # important to do because there might new models on the server!
    logger.info("  Checking for any new models we need")
    models_downloaded = download_models()
    logger.info(f"    models downloaded: {models_downloaded}")
    if not models_downloaded:
        sys.exit(1)

    with Flow("story-processor") as flow:
        if WORKER_COUNT > 1:
            flow.executor = LocalDaskExecutor(scheduler="threads", num_workers=WORKER_COUNT)
        # read parameters
        data_source_name = "unposted"
        start_time = Parameter("start_time", default=time.time())
        stories_per_page = Parameter("stories_per_page", default=DEFAULT_STORIES_PER_PAGE)
        logger.info("    will request {} stories/page".format(stories_per_page))
        # 1. list all the project we need to work on
        projects_list = load_projects_task()
        # 2. process all the projects (in parallel)
        project_statuses = process_project_task.map(projects_list,
                                                    page_size=unmapped(stories_per_page))
        # 3. send email with results of operations
        prefect_tasks.send_project_list_email_task(project_statuses, data_source_name, start_time)
        prefect_tasks.send_project_list_slack_message_task(project_statuses, data_source_name, start_time)

    # run the whole thing
    flow.run(parameters={
        'stories_per_page': DEFAULT_STORIES_PER_PAGE,
        'start_time': time.time(),
    })
