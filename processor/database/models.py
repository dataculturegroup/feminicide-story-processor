import datetime as dt
import logging
from typing import Dict

import mcmetadata.urls as urls
from dateutil.parser import parse
from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class Story(Base):
    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer)
    model_id: Mapped[int] = mapped_column(Integer)
    model_score: Mapped[float] = mapped_column(Float)
    model_1_score: Mapped[float] = mapped_column(Float)
    model_2_score: Mapped[float] = mapped_column(Float)
    published_date: Mapped[dt.datetime] = mapped_column(DateTime)
    queued_date: Mapped[dt.datetime] = mapped_column(DateTime)
    processed_date: Mapped[dt.datetime] = mapped_column(DateTime)
    posted_date: Mapped[dt.datetime] = mapped_column(DateTime)
    # ToDo: need to add a migration so this is Nullable
    above_threshold: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    normalized_url: Mapped[str] = mapped_column(String)

    def __repr__(self):
        return "<Story id={} source={}>".format(self.id, self.source)

    @staticmethod
    def from_source(story: Dict, source: str):
        db_story = Story()
        db_story.url = story["url"]
        db_story.normalized_url = urls.normalize_url(
            story["url"]
        )  # this will help in de-duplication
        db_story.source = source
        # carefully parse date, with fallback to today so we at least get something close to right
        use_fallback_date = False
        try:
            if not isinstance(story["publish_date"], dt.datetime) and not isinstance(
                story["publish_date"], dt.date
            ):
                db_story.published_date = parse(story["publish_date"])
            elif story["publish_date"] is not None:
                db_story.published_date = story["publish_date"]
            else:
                use_fallback_date = True
        except Exception:
            use_fallback_date = True
        if use_fallback_date:
            db_story.published_date = dt.datetime.now()
            logger.warning(
                "Used today as publish date for story that didn't have date ({}) on it: {}".format(
                    story["publish_date"], db_story.url
                )
            )
        return db_story


class ProjectHistory(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    latest_date_mc: Mapped[dt.datetime] = mapped_column(DateTime)
    latest_date_nc: Mapped[dt.datetime] = mapped_column(DateTime)
    latest_date_wm: Mapped[dt.datetime] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)

    def __repr__(self):
        return "<ProjectHistory id={}>".format(self.id)
