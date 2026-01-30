import json
import logging
import os
import time
from json.decoder import JSONDecodeError
from typing import Dict, List

import requests
from sqlalchemy.orm.session import Session

import processor.database as database
import processor.database.stories_db as stories_db
import processor.entities as entities
import processor.projects as projects
import processor.util as util
from processor import path_to_log_dir
from processor.celery import app

logger = logging.getLogger(__name__)  # get_task_logger(__name__)
logFormatter = logging.Formatter(
    "[%(levelname)s %(threadName)s] - %(asctime)s - %(name)s - : %(message)s"
)
fileHandler = logging.FileHandler(
    os.path.join(path_to_log_dir, "tasks-{}.log".format(time.strftime("%Y%m%d-%H%M%S")))
)
fileHandler.setFormatter(logFormatter)
logger.addHandler(fileHandler)

# central email alert server just uses a list of all the entities, so important to filter here to ones we care about
# ie. people, places, dates/times
ACCEPTED_ENTITY_TYPES = [
    "PERSON",
    "PER",
    "GPE",
    "LOC",
    "FAC",
    "DATE",
    "TIME",  # TODO: figure out which models there are from and comment
    "C_DATE",
    "C_AGE",  # our custom extracted types
    "PS",
    "DT",
    "TI",
    "LC",
]  # korean types


def _add_confidence_to_stories(
    session: Session, project: Dict, stories: List[Dict]
) -> List[Dict]:
    if not stories:
        return stories
    probs = projects.classify_stories(project, stories)
    for idx, s in enumerate(stories):
        s["confidence"] = probs["model_scores"][idx]
        # adding in some extra stuff for logging only (they get removed in `prep_stories_for_posting`)
        s["model_score"] = probs["model_scores"][idx]
        s["model_1_score"] = (
            probs["model_1_scores"][idx]
            if probs["model_1_scores"] is not None
            else None
        )
        s["model_2_score"] = (
            probs["model_2_scores"][idx]
            if probs["model_2_scores"] is not None
            else None
        )
    # keep an auditable log in our own local database
    stories_db.update_stories_processed_date_score(session, stories)
    return stories


def add_entities_to_stories(stories: List[Dict]):
    for s in stories:
        story_entities = None
        if entities.server_address_set():
            try:
                response = entities.from_content(
                    f"{s['title']} {s['story_text']}", s["language"], s["url"]
                )
                story_entities = [
                    item["text"].lower()
                    for item in response["results"]["entities"]
                    if item["type"] in ACCEPTED_ENTITY_TYPES
                ]
            except JSONDecodeError:
                # the entity extractor failed, so don't return any entities
                story_entities = None
            except Exception as e:
                # something else happened (maybe unsupported language?), so log it but also continue
                logger.warning(e)
                story_entities = None
        s["entities"] = story_entities
    return stories


@app.task(serializer="json", bind=True)
def classify_and_post_worker(self, project: Dict, stories: List[Dict]):
    """
    Take a page of stories matching a project and run them through the classifier for that project.
    :param self:
    :param project: a dict with the project info from the main server
    :param stories: a list of stories, each with the following properties (see `projects.prep_stories_for_posting`):
                    * `story_text`: raw text content for classification
                    * `language`: two letter language story is in
                    * `media_id`: a unique id for the publisher
                    * `media_url`: the url of the publisher
                    * `media_name`: the name of the publisher
                    * `publish_date`: the extracted date of publication for the story
                    * `story_tags`: a list of Dicts with metadata about story (entities, etc)
                    * `title`: the extracted title of the story
                    * `url`: the full URL of the story

    """
    try:
        logger.debug(
            "{}: classify {} stories (model {})".format(
                project["id"], len(stories), project["language_model_id"]
            )
        )
        # now classify the stories again the model specified for the project (this cleans up the story dicts too)
        if not stories:
            logger.debug(
                "{}: skipping cowardly attempt to classify empty stories".format(
                    project["id"]
                )
            )
            return

        Session = database.get_session_maker()
        with Session() as session:
            stories_to_send = _add_confidence_to_stories(session, project, stories)
            for s in stories_to_send:
                logger.debug(
                    "  classify: {}/{} - {}".format(
                        project["id"],
                        project["language_model_id"],
                        s["confidence"],
                    )
                )
            # only stories above project score threshold should be posted
            stories_to_send = projects.remove_low_confidence_stories(
                project.get("min_confidence", 0), stories_to_send
            )
            # remove any duplicates based on title & story (we've seen this with _slightly_ diff URLs)
            stories_to_send = util.remove_duplicate_by_title_media_id(stories_to_send)
            # pull out entities, if there is an env-var to a server set (only do this on above-threshold stories)
            stories_to_send = add_entities_to_stories(stories_to_send)
            # remove data we aren't going to send to the server (and log)
            stories_to_send = projects.prep_stories_for_posting(
                project, stories_to_send
            )
            if (
                projects.LOG_LAST_POST_TO_FILE
            ):  # helpful for debugging (the last project post will be written to a file)
                with open(
                    os.path.join(
                        path_to_log_dir,
                        "{}-all-stories-{}.json".format(
                            project["id"], time.strftime("%Y%m%d-%H%M%S")
                        ),
                    ),
                    "w",
                    encoding="utf-8",
                ) as f:
                    json.dump(stories_to_send, f, ensure_ascii=False, indent=4)
            # mark the stories in the local DB that we intend to send
            stories_db.update_stories_above_threshold(session, stories_to_send)
            # ToDo: if we change how stories are created to not have a threshold value set at init, then here we
            #  would need to also mark stories below threshold as such in the local DB
            # now actually post them (in chunks just to make sure no single page is too big and causes a HTTP 413 error)
            logger.info(
                "{}: {} stories to post".format(project["id"], len(stories_to_send))
            )
            for page_to_send in util.chunks(stories_to_send, 100):
                projects.post_results(project, page_to_send)
                for (
                    s
                ) in (
                    page_to_send
                ):  # for auditing, keep a log in the container of the results posted to main server
                    logger.debug(
                        "  post: {}/{} - {}".format(
                            s["project_id"],
                            s["language_model_id"],
                            s["confidence"],
                        )
                    )
                # and track that we posted the stories that we did in our local debug DB
                stories_db.update_stories_posted_date(session, page_to_send)
    except requests.exceptions.HTTPError as err:
        # on failure requeue to try again
        logger.warning(
            "{}: Failed to post {} results".format(project["id"], len(stories))
        )
        # logger.exception(err) #Sentry logging ignored
        raise self.retry(exc=err)
    except Exception as exc:
        # only failure here is the classifier not loading? probably we should try again... feminicide server holds state
        logger.warning(
            "{}: Failed to label {} stories".format(project["id"], len(stories))
        )
        logger.exception(exc)
        raise self.retry(exc=exc)
