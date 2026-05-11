import copy
import datetime as dt
import logging
from typing import Dict, List

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.session import Session
from sqlalchemy.sql import func

from processor.database.models import Story

logger = logging.getLogger(__name__)


def project_story_normalized_urls(
    session: Session, project: Dict, last_n_days: int
) -> List[str]:
    """
    This is helpful for de-duplication. We want to find out which stories were fetched and processed for this
    project in the last N days so that we can ignore them if we fetch those URLs again.
    :param session:
    :param project:
    :param last_n_days:
    :return: a list of normalized URLs, ready to be compared against other URLs
    """
    # use a time window to look for recent stories
    last_n_days_filter = Story.queued_date > (
        dt.datetime.now() - dt.timedelta(days=last_n_days)
    )
    # limit stories to just ones we fetched for this project
    project_id_filter = Story.project_id == project["id"]
    # make sure they weren't processed already
    not_processed_filter = Story.processed_date.is_not(None)
    matching = (
        session.query(Story)
        .filter(project_id_filter)
        .filter(last_n_days_filter)
        .filter(not_processed_filter)
        .all()
    )
    return [s.normalized_url for s in matching]


def add_stories(
    session: Session, source_story_list: List[Dict], project: Dict, source: str
) -> List[Dict]:
    """
    Logging: Track metadata about all the stories we process we, so we can audit it later (like a log file).
    :param session:
    :param source_story_list:
    :param project:
    :param source:
    :return: list of ids of objects inserted
    """
    ignored_count = 0
    new_source_story_list = copy.copy(source_story_list)
    now = dt.datetime.now()
    for discovered_story in new_source_story_list:
        db_story = Story.from_source(discovered_story, source)
        db_story.project_id = project["id"]
        db_story.model_id = project["language_model_id"]
        db_story.queued_date = now
        # ToDo: this should really be changed so it doesn't mark either until it is run against a model
        db_story.above_threshold = False
        discovered_story["db_story"] = db_story
    # now insert in batch to the database
    for s in new_source_story_list:
        try:
            session.add(s["db_story"])
            session.commit()
        except IntegrityError:
            # duplicate story, so just ignore it
            session.rollback()
            logger.debug(
                f"Duplicate story found for project {project['id']}: {s['url']}"
            )
            del s["db_story"]
            ignored_count += 1
    # only keep ones that inserted correctly
    new_source_story_list = [
        s for s in new_source_story_list if ("db_story" in s) and s["db_story"].id
    ]
    for s in new_source_story_list:
        s["log_db_id"] = s[
            "db_story"
        ].id  # keep track of the db id, so we can use it later to update this story
    for s in new_source_story_list:  # free the DB objects back for GC
        if "db_story" in s:  # a little extra safety
            del s["db_story"]
    logger.info(f"  ignored {ignored_count} stories as duplicates")
    return new_source_story_list


def update_stories_processed_date_score(session: Session, stories: List) -> None:
    """
    Logging: Once we have run the stories through the classifier models we want to save the scores.
    :param session:
    :param stories:
    :return:
    """
    now = dt.datetime.now()
    for s in stories:
        if "log_db_id" in s:  # more gracefully fail in test scenarios
            session.execute(
                update(Story)
                .where(Story.id == s["log_db_id"])
                .values(
                    model_score=s["model_score"],
                    model_1_score=s["model_1_score"],
                    model_2_score=s["model_2_score"],
                    processed_date=now,
                )
            )  # [updated]
    session.commit()


def update_stories_above_threshold(session: Session, stories: List) -> None:
    """
    Logging: Also keep track which stories were above the classifier score threshold on the project right now.
    Ones above should be sent to the server.
    :param session:
    :param stories:
    :return:
    """
    for s in stories:
        session.execute(
            update(Story).where(Story.id == s["log_db_id"]).values(above_threshold=True)
        )  # [updated]
    session.commit()


def update_stories_posted_date(session: Session, stories: List) -> None:
    """
    Logging: Keep track of when we sent stories above threshold to the main server.
    :param session:
    :param stories:
    :return:
    """
    now = dt.datetime.now()
    for s in stories:
        session.execute(
            update(Story).where(Story.id == s["log_db_id"]).values(posted_date=now)
        )  # [updated]
    session.commit()


def recent_stories(
    session: Session, project_id: int, above_threshold: bool, limit: int = 5
) -> List[Story]:
    """
    UI: show a list of the most recent stories we have processed
    :param session:
    :param project_id:
    :param above_threshold:
    :param limit:
    :return:
    """
    earliest_date = dt.date.today() - dt.timedelta(days=7)
    result = session.execute(
        select(Story)
        .where(
            (Story.project_id == project_id)
            & (Story.above_threshold == above_threshold)
            & (Story.published_date > earliest_date)
        )
        .order_by(func.random())
        .limit(limit)
    )
    return result.scalars().all()


def _stories_by_date_col(
    session: Session,
    column_name: str,
    project_id: int = None,
    platform: str = None,
    above_threshold: bool = None,
    is_posted: bool = None,
    limit: int = 30,
) -> List:
    earliest_date = dt.date.today() - dt.timedelta(days=limit)
    clauses = []
    if project_id is not None:
        clauses.append("(project_id={})".format(project_id))
    if platform is not None:
        clauses.append("(source='{}')".format(platform))
    if above_threshold is not None:
        clauses.append(
            "(above_threshold is {})".format("True" if above_threshold else "False")
        )
    if is_posted is not None:
        clauses.append("(posted_date {} Null)".format("is not" if is_posted else "is"))
    query = (
        "select " + column_name + "::date as day, count(1) as stories from stories "
        "where ("
        + column_name
        + " is not Null) and ("
        + column_name
        + " >= '{}'::DATE) AND {} "
        "group by 1 order by 1 DESC".format(earliest_date, " AND ".join(clauses))
    )
    return _run_query(session, query)


def stories_by_posted_day(
    session: Session,
    project_id: int = None,
    platform: str = None,
    above_threshold: bool = True,
    is_posted: bool = None,
    limit: int = 45,
) -> List:
    return _stories_by_date_col(
        session,
        "processed_date",
        project_id,
        platform,
        above_threshold,
        is_posted,
        limit,
    )


def stories_by_processed_day(
    session: Session,
    project_id: int = None,
    platform: str = None,
    above_threshold: bool = None,
    is_posted: bool = None,
    limit: int = 45,
) -> List:
    return _stories_by_date_col(
        session,
        "processed_date",
        project_id,
        platform,
        above_threshold,
        is_posted,
        limit,
    )


def stories_by_published_day(
    session: Session,
    project_id: int = None,
    platform: str = None,
    above_threshold: bool = None,
    is_posted: bool = None,
    limit: int = 30,
) -> List:
    return _stories_by_date_col(
        session,
        "published_date",
        project_id,
        platform,
        above_threshold,
        is_posted,
        limit,
    )


def _run_query(session: Session, query: str) -> List:
    results = session.execute(text(query))
    data = []
    for row in results:
        data.append(row._mapping)
    return data


def _run_count_query(session: Session, query: str) -> int:
    data = _run_query(session, query)
    return data[0]["count"]


def unposted_above_story_count(
    session: Session, project_id: int, limit: int = None
) -> int:
    """
    UI: How many stories about threshold have *not* been sent to main server (should be zero!).
    """
    date_clause = "(posted_date is not Null)"
    if limit:
        earliest_date = dt.date.today() - dt.timedelta(days=limit)
        date_clause += " AND (posted_date >= '{}'::DATE)".format(earliest_date)
    query = "select count(1) from stories where project_id={} and above_threshold is True and {}".format(
        project_id, date_clause
    )
    return _run_count_query(session, query)


def posted_above_story_count(session: Session, project_id: int) -> int:
    """
    UI: How many stories above threshold have we sent to the main server (like all should be)
    :param project_id:
    :return:
    """
    query = (
        "select count(1) from stories "
        "where project_id={} and posted_date is not Null and above_threshold is True".format(
            project_id
        )
    )
    return _run_count_query(session, query)


def below_story_count(session: Session, project_id: int) -> int:
    """
    UI: How many stories total were below threshold (should be same as uposted_stories)
    :param project_id:
    :return:
    """
    query = "select count(1) from stories where project_id={} and above_threshold is False".format(
        project_id
    )
    return _run_count_query(session, query)


def unposted_stories(session: Session, project_id: int, limit: int):
    """
    How many stories were not posted to hte main server (should be same as below_story_count)
    :return:
    """
    earliest_date = dt.date.today() - dt.timedelta(days=limit)
    query = (
        "select * from stories "
        "where project_id={} and posted_date is Null and (posted_date >= '{}'::DATE) and above_threshold is True".format(
            project_id, earliest_date
        )
    )
    """
    session = Session()
    q = session.query(Story). \
        filter(Story.project_id == project_id). \
        filter(Story.above_threshold is True). \
        filter(Story.posted_date is None)
    return q.all()
    """
    return _run_query(session, query)


def project_binned_model_scores(session: Session, project_id: int) -> List:
    query = """
        select ROUND(CAST(model_score as numeric), 1) as value, count(1) as frequency
        from stories
        where project_id={} and model_score is not NULL
        group by 1
        order by 1
    """.format(project_id)
    return _run_query(session, query)


def delete_old_stories(session: Session, age: int = 62) -> None:
    """
    Delete stories that have been posted more than 62 days ago.
    """
    today = dt.datetime.now()
    date_cutoff = today - dt.timedelta(days=age)
    session.execute(delete(Story).where(Story.queued_date < date_cutoff))
    session.commit()
