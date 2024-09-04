import os
from unittest.mock import patch

import pytest
from utils import (
    verify_project_history,
    verify_stories_table,
    verify_story_threshold_counts,
)

import scripts.queue_newscatcher_stories as newscatcher
import scripts.tasks as tasks
from processor import SOURCE_NEWSCATCHER, projects
from processor.tasks.classification import classify_and_post_worker

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.join(base_dir, "test", "models")


@pytest.mark.celery()
def test_queue_newscatcher_stories(
    mock_load_projects,
    mock_fetch_project_stories,
    mock_model_list,
    celery_app,
    db_session_maker,
):
    # pickle has trouble without import
    from models.stubbed_1_model import NoOpLogisticRegression  # noqa: F401

    projects.REALLY_POST = False  # We don't want to post any results to the main server

    # Create a session for our test
    Session = db_session_maker
    db_session = Session()

    # 1. list the sample project and get models
    projects_list = newscatcher.load_projects()
    assert projects_list == mock_load_projects

    # verify history got added to database
    verify_project_history(db_session)

    # 2. fetch all the sample stories for sample project
    all_stories = mock_fetch_project_stories["newscatcher"].return_value
    unique_url_count = len(set([s["url"] for s in all_stories]))
    assert unique_url_count == 100
    assert len(all_stories) == 100

    # 3. fetch webpage text and parse all the stories (No stories should fail)
    stories_with_text = newscatcher.fetch_text(all_stories)
    assert len(stories_with_text) == 100

    # 4. post stories for classification, verify expected results
    results_data = tasks.queue_stories_for_classification(
        projects_list, stories_with_text, SOURCE_NEWSCATCHER
    )

    # verify rows were added to the database and we achieved expected result
    verify_stories_table(db_session)
    assert results_data["stories"] == 100

    with patch("processor.classifiers.MODEL_DIR", TEST_DIR):
        classify_and_post_worker.apply(
            args=[projects_list[0], stories_with_text]
        )  # Tasks don't get run
        # urgently, would be useful not to have to call the task directly

        # Verify threshold counts
        verify_story_threshold_counts(db_session, project_id=1111)