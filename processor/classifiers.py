import os
import logging
import pickle
from typing import Dict, List
import tensorflow_hub as hub

from processor import base_dir

logger = logging.getLogger(__name__)

test_fixture_dir = os.path.join(base_dir, "processor", "test", "fixtures")

files_dir = os.path.join(base_dir, "files")
model_dir = os.path.join(base_dir, "files", "models")

NAIVE_BAYES_MODEL = 'naive_bayes'
SENTENCE_EMBEDDINGS_MODEL = 'sentence_embeddings'

MODELS = {
    'en_usa': dict(type=NAIVE_BAYES_MODEL, tfidf_vectorizer='usa_vectorizer.p', nb_model='usa_model.p'),
    'es_uruguay': dict(type=NAIVE_BAYES_MODEL, tfidf_vectorizer='uruguay_vectorizer.p', nb_model='uruguay_model.p'),
    # the model file is downloaded to this location by the deploy hook that runs scripts/download-models.sh
    'en_aapf': dict(type=SENTENCE_EMBEDDINGS_MODEL, tfhub_model_path='/tmp/models/', local_model='usa_model_aapf.p')
}


class Classifier:

    def __init__(self, model_config: Dict, project: Dict):
        self.config = model_config
        self.project = project
        self._init()

    def _init(self):
        if self.config['type'] == NAIVE_BAYES_MODEL:
            with open(os.path.join(model_dir, MODELS[self.project['model_name']]['tfidf_vectorizer']), 'rb') as v:
                self.tfidf_vectorizer = pickle.load(v)
            with open(os.path.join(model_dir, MODELS[self.project['model_name']]['nb_model']), 'rb') as m:
                self.nb_model = pickle.load(m)
        elif self.config['type'] == SENTENCE_EMBEDDINGS_MODEL:
            try:
                self.embed = hub.load(self.config['tfhub_model_path'])  # this will cache to a local dir
                with open(os.path.join(model_dir, self.config['local_model']), 'rb') as m:
                    self.lr_model = pickle.load(m)
            except OSError:
                # probably the cached SavedModel doesn't exist anymore
                logger.error("Can't load model from {}".format(self.config['tfhub_model_path']))
        else:
            raise RuntimeError("Unknown model {} for project {}".format(self.config['type'], self.project['id']))

    def classify(self, stories: List[Dict]) -> List[float]:
        story_texts = [s['story_text'] for s in stories]
        predictions = []
        if self.config['type'] == NAIVE_BAYES_MODEL:
            vectorized_data = self.tfidf_vectorizer.transform(story_texts)
            predictions = self.nb_model.predict_proba(vectorized_data)
        elif self.config['type'] == SENTENCE_EMBEDDINGS_MODEL:
            vectorized_data = self.embed(story_texts)
            predictions = self.lr_model.predict_proba(vectorized_data)
        true_probs = predictions[:, 1]
        return true_probs


def for_project(project: Dict) -> Classifier:
    """
    This is where we would download a classifier, as needed, from the main server based on the URL info
    in the project config passed in. This is a factory method.
    """
    if 'language' not in project:
        logger.error('No language specified on {}'.format(project['id']))
    project['model_name'] = _model_name_for_project(project)
    if project['model_name'] not in MODELS.keys():
        logger.error('Invalid model_name specified on {}'.format(project['id']))
    return Classifier(MODELS[project['model_name']], project)


def _model_name_for_project(project: Dict) -> str:
    """
    Pick the right model based on keys from the server. Keep in line with central server constants.
    """
    if project['language_model'] == "English (Default)":
        model_name = 'en_usa'
    elif project['language_model'] == "Spanish (Default)":
        model_name = 'es_uruguay'
    elif project['language_model'] == "US English: Black women killed in police violence":
        model_name = 'en_aapf'
    else:
        model_name = 'en_usa'  # default to english language
    return model_name
