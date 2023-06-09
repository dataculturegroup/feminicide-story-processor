import logging
import datetime as dt
from typing import List, Dict
import time
from prefect import Flow, Parameter, task, unmapped
from prefect.executors import LocalDaskExecutor
import processor
import processor.database.stories_db as stories_db
import processor.database.projects_db as projects_db
from processor.classifiers import download_models
from processor import get_mc_legacy_client
import processor.projects as projects
import processor.tasks as tasks
import scripts.tasks as prefect_tasks

DEFAULT_STORIES_PER_PAGE = 150  # I found this performs poorly if set too high
DEFAULT_MAX_STORIES_PER_PROJECT = 10000  # make sure we don't do too many stories each cron run (for testing)

WORKER_COUNT = 6  # scale of parallel processing (of project queries)

# use this to make sure we don't fall behind on recent stories, even if a project query is producing more than
# DEFAULT_MAX_STORIES_PER_PROJECT stories a day
DEFAULT_DAY_WINDOW = 3


@task(name='load_projects')
def load_projects_task() -> List[Dict]:
    project_list = projects.load_project_list(force_reload=True, overwrite_last_story=False)
    logger.info("  Checking {} projects".format(len(project_list)))
    #return [p for p in project_list if p['id'] == 166]
    return project_list


@task(name='process_project')
def process_project_task(project: Dict, page_size: int, max_stories: int) -> Dict:
    mc = get_mc_legacy_client()
    project_last_processed_stories_id = project['local_processed_stories_id']
    project_email_message = ""
    logger.info("Checking project {}/{} (last processed_stories_id={})".format(project['id'], project['title'],
                                                                               project_last_processed_stories_id))
    logger.debug("  {} stories/page up to {}".format(page_size, max_stories))
    project_email_message += "Project {} - {}:\n".format(project['id'], project['title'])
    # setup queries to filter by language too so we only get stories the model can process
    q = "({}) AND language:{} AND tags_id_media:({})".format(
        project['search_terms'],
        project['language'],
        " ".join([str(tid) for tid in project['media_collections']]))
    now = dt.datetime.now()
    start_date = now - dt.timedelta(days=DEFAULT_DAY_WINDOW)  # err on the side of recentness over completeness
    fq = mc.dates_as_query_clause(start_date, now)
    # debug output total story count, removed because it slows things down
    # total_stories = mc.storyCount(q, fq)['count']
    # logger.info("  project {} - total {} stories".format(project['id'], total_stories))
    # return dict(email_text="", stories=total_stories, pages=0) # helpful for debugging
    # page through any new stories
    story_count = 0
    page_count = 0
    more_stories = True
    while more_stories and (story_count < max_stories):
        try:
            page_of_stories = mc.storyList(q, fq, last_processed_stories_id=project_last_processed_stories_id,
                                           text=True, rows=page_size)
            logger.info("    {} - page {}: ({}) stories".format(project['id'], page_count, len(page_of_stories)))
        except Exception as e:
            logger.error("  Story list error on project {}. Skipping for now. {}".format(project['id'], e))
            more_stories = False
            continue  # fail gracefully by going to the next project; maybe next cron run it'll work?
        if len(page_of_stories) > 0:
            for s in page_of_stories:
                s['source'] = processor.SOURCE_MEDIA_CLOUD
            page_count += 1
            story_count += len(page_of_stories)
            # and log that we got and queued them all
            stories_to_queue = stories_db.add_stories(page_of_stories, project, processor.SOURCE_MEDIA_CLOUD)
            tasks.classify_and_post_worker.delay(project, stories_to_queue)
            project_last_processed_stories_id = stories_to_queue[-1]['processed_stories_id']
            # important to write this update now, because we have queued up the task to process these stories
            # the task queue will manage retrying with the stories if it fails with this batch
            projects_db.update_history(project['id'], last_processed_stories_id=project_last_processed_stories_id)
        else:
            more_stories = False
    logger.info("  queued {} stories for project {}/{} (in {} pages)".format(story_count, project['id'],
                                                                             project['title'], page_count))
    #  add a summary to the email we are generating
    warnings = ""
    if story_count > (max_stories * 0.8):  # try to get our attention in the email
        warnings += "(⚠️️️ query might be too broad)"
    project_email_message += "    found {} new stories past {} (over {} pages) {}\n\n".format(
        story_count, project_last_processed_stories_id, page_count, warnings)
    return dict(
        email_text=project_email_message,
        stories=story_count,
        pages=page_count,
    )


if __name__ == '__main__':

    logger = logging.getLogger(__name__)
    logger.info("Starting {} story fetch job".format(processor.SOURCE_MEDIA_CLOUD))

    # important to do because there might new models on the server!
    logger.info("  Checking for any new models we need")
    download_models()

    with Flow("story-processor") as flow:
        if WORKER_COUNT > 1:
            flow.executor = LocalDaskExecutor(scheduler="threads", num_workers=WORKER_COUNT)
        # read parameters
        data_source_name = Parameter("data_source", default="")
        stories_per_page = Parameter("stories_per_page", default=DEFAULT_STORIES_PER_PAGE)
        max_stories_per_project = Parameter("max_stories_per_project", default=DEFAULT_MAX_STORIES_PER_PROJECT)
        start_time = Parameter("start_time", default=time.time())
        logger.info("    will request {} stories/page (up to {})".format(stories_per_page, max_stories_per_project))
        # 1. list all the project we need to work on
        projects_list = load_projects_task()
        # 2. process all the projects (in parallel)
        project_statuses = process_project_task.map(projects_list,
                                                    page_size=unmapped(stories_per_page),
                                                    max_stories=unmapped(max_stories_per_project))
        # 3. send email with results of operations
        prefect_tasks.send_project_list_email_task(project_statuses, data_source_name, start_time)
        prefect_tasks.send_project_list_slack_message_task(project_statuses, data_source_name, start_time)

        

    # run the whole thing
    flow.run(parameters={
        'stories_per_page': DEFAULT_STORIES_PER_PAGE,
        'max_stories_per_project': DEFAULT_MAX_STORIES_PER_PROJECT,
        'data_source': processor.SOURCE_MEDIA_CLOUD,
        'start_time': time.time(),
    })