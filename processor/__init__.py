import logging
import os
import sys
from typing import Dict

import mediacloud.api
from celery import signals
from dotenv import load_dotenv
from sentry_sdk import init
from sentry_sdk.integrations.logging import ignore_logger

VERSION = "4.4.3"
SOURCE_GOOGLE_ALERTS = "google-alerts"
SOURCE_MEDIA_CLOUD = "media-cloud"
SOURCE_NEWSCATCHER = "newscatcher"
SOURCE_WAYBACK_MACHINE = "wayback-machine"
PLATFORMS = [
    SOURCE_GOOGLE_ALERTS,
    SOURCE_MEDIA_CLOUD,
    SOURCE_NEWSCATCHER,
    SOURCE_WAYBACK_MACHINE,
]

load_dotenv()  # load config from .env file (local) or env vars (production)


def before_send(event, hint):
    if "exc_info" in hint:
        exc_type, exc_value, _ = hint["exc_info"]

        ignored_exceptions = (TimeoutError, ConnectionError, AttributeError)
        if isinstance(exc_value, ignored_exceptions):
            return None

    return event


base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path_to_log_dir = os.path.join(base_dir, "logs")

# set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)
logger.info("------------------------------------------------------------------------")
logger.info("Starting up Feminicide Story Processor v{}".format(VERSION))
# supress annoying "not enough comments" and "using custom extraction" notes# logger = logging.getLogger(__name__)
loggers_to_skip = [
    "trafilatura.core",
    "trafilatura.metadata",
    "readability.readability",
    "trafilatura.readability_lxml",
    "trafilatura.htmlprocessing",
    "trafilatura.xml",
]
for item in loggers_to_skip:
    logging.getLogger(item).setLevel(logging.WARNING)

# read in environment variables
MC_API_TOKEN = os.environ.get("MC_API_TOKEN", None)  # sensitive, so don't log it
if MC_API_TOKEN is None:
    logger.error(
        "  ❌ No MC_API_TOKEN env var specified. Pathetically refusing to start!"
    )
    sys.exit(1)

BROKER_URL = os.environ.get("BROKER_URL", None)
if BROKER_URL is None:
    logger.warning(
        "  ⚠️ No BROKER_URL env var specified. Using sqlite, which will perform poorly"
    )
    BROKER_URL = "db+sqlite:///results.sqlite"
# logger.info("  Queue at {}".format(BROKER_URL))


@signals.beat_init.connect
@signals.celeryd_init.connect
def init_sentry(**kwargs):
    SENTRY_DSN = os.environ.get("SENTRY_DSN", None)  # optional
    if SENTRY_DSN:
        from sentry_sdk.integrations.celery import CeleryIntegration

        init(
            dsn=SENTRY_DSN,
            release=VERSION,
            before_send=before_send,
            enable_tracing=True,
            traces_sample_rate=1.0,
            profiles_sample_rate=1.0,
            integrations=[CeleryIntegration(monitor_beat_tasks=True)],
        )
        ignore_logger("scrapy.core.scraper")
        ignore_logger("scrapy.downloadermiddlewares.retry")
        ignore_logger("trafilatura.core")
        ignore_logger("htmldate.utils")
        logger.info("  SENTRY_DSN: {}".format(SENTRY_DSN))
    # else:
    # logger.info("  Not logging errors to Sentry")

init_sentry()

FEMINICIDE_API_URL = os.environ.get("FEMINICIDE_API_URL", None)
if FEMINICIDE_API_URL is None:
    logger.error(
        "  ❌ No FEMINICIDE_API_URL is specified. Bailing because we can't list projects to run!"
    )
    sys.exit(1)
# else:
#    logger.info("  Config server at at {}".format(FEMINICIDE_API_URL))

FEMINICIDE_API_KEY = os.environ.get("FEMINICIDE_API_KEY", None)
if FEMINICIDE_API_KEY is None:
    logger.error(
        "  ❌ No FEMINICIDE_API_KEY is specified. Bailing because we can't send things to the main server without one"
    )
    sys.exit(1)

SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", None)
if SQLALCHEMY_DATABASE_URI is None:
    logger.warning(
        "  ⚠️ ️No SQLALCHEMY_DATABASE_URI is specified. Using sqlite which will perform poorly"
    )


ENTITY_SERVER_URL = os.environ["ENTITY_SERVER_URL"]
if ENTITY_SERVER_URL is None:
    logger.warning(
        "  ⚠️ No ENTITY_SERVER_URL is specified. You won't get entities in the stories sent to the  main server."
    )


NEWSCATCHER_API_KEY = os.environ["NEWSCATCHER_API_KEY"]
if NEWSCATCHER_API_KEY is None:
    logger.warning(
        "  ⚠️ No NEWSCATCHER_API_KEY is specified. We won't be fetching from Newscatcher."
    )

SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", None)
if SLACK_APP_TOKEN is None:
    logger.warning(
        "  ⚠️ No SLACK_APP_TOKEN env var specified. We won't be sending slack updates."
    )

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", None)
if SLACK_BOT_TOKEN is None:
    logger.warning(
        "  ⚠️ No SLACK_BOT_TOKEN env var specified. We won't be sending slack updates."
    )

SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", None)
if SLACK_CHANNEL_ID is None:
    logger.warning(
        "  ⚠️ No CHANNEL_ID env var specified. We won't be sending slack updates."
    )


def get_mc_directory_client() -> mediacloud.api.DirectoryApi:
    """
    A central place to get the Media Cloud client
    :return: an media cloud client with the API key from the environment variable
    """
    return mediacloud.api.DirectoryApi(MC_API_TOKEN)


def get_mc_client() -> mediacloud.api.SearchApi:
    """
    A central place to get the Media Cloud search client
    :return: an admin media cloud client with the API key from the environment variable
    """
    mc = mediacloud.api.SearchApi(MC_API_TOKEN)
    mc.TIMEOUT_SECS = 120  # some of the queries run against giant collections
    return mc


def is_slack_configured() -> bool:
    return (
        (os.environ.get("SLACK_APP_TOKEN", None) is not None)
        and (os.environ.get("SLACK_BOT_TOKEN", None) is not None)
        and (os.environ.get("SLACK_CHANNEL_ID", None) is not None)
    )


def is_email_configured() -> bool:
    return (
        (os.environ.get("SMTP_USER_NAME", None) is not None)
        and (os.environ.get("SMTP_PASSWORD", None) is not None)
        and (os.environ.get("SMTP_ADDRESS", None) is not None)
        and (os.environ.get("SMTP_PORT", None) is not None)
        and (os.environ.get("SMTP_FROM", None) is not None)
        and (os.environ.get("NOTIFY_EMAILS", None) is not None)
    )


def get_email_config() -> Dict:
    return dict(
        user_name=os.environ.get("SMTP_USER_NAME", None),
        password=os.environ.get("SMTP_PASSWORD", None),
        address=os.environ.get("SMTP_ADDRESS", None),
        port=os.environ.get("SMTP_PORT", None),
        from_address=os.environ.get("SMTP_FROM", None),
        notify_emails=os.environ.get("NOTIFY_EMAILS", "").split(","),
    )


def get_slack_config() -> Dict:
    return dict(
        app_token=SLACK_APP_TOKEN,
        bot_token=SLACK_BOT_TOKEN,
        channel_id=SLACK_CHANNEL_ID,
    )


def disable_package_loggers() -> None:
    pkg_loggers = [
        "trafilatura.core",
        "trafilatura.metadata",
        "readability.readability",
        "trafilatura.readability_lxml",
        "trafilatura.htmlprocessing",
        "trafilatura.xml",
        "mcmetadata.languages",
    ]
    for pkg_logger in pkg_loggers:
        logging.getLogger(pkg_logger).setLevel(logging.WARNING)

    # Ignore Tensorflow logging
    os.environ["TF_CPP_MIN_LOG_LEVEL "] = "2"
