import os
import sys
import json
import openai
import asyncio
import aiofiles
import csv
from dotenv import load_dotenv
import atexit
from typing import Any, List, Optional, Callable, Dict
import spacy
import logging
import datetime
from tiktoken import get_encoding
import subprocess
import groq
from clients.coze import AsyncCoze
import spacy
import errant

# Ensure you have loaded the spaCy model at the start of your script
nlp = spacy.load("en_core_web_sm")
annotator = errant.load("en")


# Load environment variables from .env file
load_dotenv()


# TUNABLE CONFIGS

# CONFIGS: MODEL
OPENAI_MODELS = [
    "gpt-3.5-turbo",
]

OPENAI_JSON_MODE_SUPPORTED_MODELS = [
    "gpt-3.5-turbo-1106",
    "gpt-4-1106-preview",
]

LOCAL_LLM_MODELS = [
    "llama-2-7b-chat.Q8_0.gguf",
]

TOGETHER_AI_MODELS = [
    "togethercomputer/Llama-2-7B-32K-Instruct",
    "mistralai/Mixtral-8x7B-Instruct-v0.1",
]

GROQ_MODELS = [
    "gemma-7b-it",
    "llama2-70b-4096",
    "mixtral-8x7b-32768",
]

# coze bot ids
COZE_BOTS = [
    "7351253103510978578",
]


QUALITY_ESTIMATION_MODEL_NAME = OPENAI_JSON_MODE_SUPPORTED_MODELS[1]


# change model here
MODEL_NAME = OPENAI_JSON_MODE_SUPPORTED_MODELS[0]
# MODEL_NAME = TOGETHER_AI_MODELS[1]
# MODEL_NAME = GROQ_MODELS[2]
# MODEL_NAME = COZE_BOTS[0]


# CONFIGS: PROMPT
# GRAMMAR_VARIANT = "standard American"
GRAMMAR_VARIANT = "British"


# TEXT_DELIMITER = "|||"
TEXT_DELIMITER = "~~~" if MODEL_NAME not in COZE_BOTS else "\n"

# CONFIGS: RAG


# NON-TUNABLE CONFIGS

# CONFIGS: INPUT PREPROCESSING
MAX_TOKENS = 1024
BATCH_SIZE_IN_TOKENS = int(MAX_TOKENS * 0.6)
VOTE_INCREASE_FACTOR = 0.02
# CHUNK_OVERLAP_IN_TOKENS = 50

# CONFIGS: API
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT", "")
LOCAL_ENDPOINT = os.getenv("LOCAL_ENDPOINT", "")
TOGETHER_ENDPOINT = os.getenv("TOGETHER_ENDPOINT", "")
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
COZE_API_KEY = os.getenv("COZE_API_KEY", "")
MAX_RETRIES = 3  # Maximum number of retries for an API call
RETRY_DELAY = 5  # Delay in seconds before retrying an API
QPM_LIMIT = 5  # Queries per minute limit

# CONFIGS: OTHERS
# ANSI escape codes for colors
RED = "\033[1;31m"
GREEN = "\033[1;32m"
YELLOW = "\033[93m"
BLUE = "\033[1;34m"
RESET = "\033[0m"


# TODO: explain before answer; give more examples; etc

QUALITY_ESTIMATION_PROMPT = """You are an AI specialized in assessing the quality of grammatical error corrections from JSON inputs. Given an input JSON with 'original' and 'corrected' keys containing lists of sentences, evaluate each corrected sentence based on:
1. Correction of spelling mistakes, punctuation errors, verb tense issues, word choice problems, and other grammatical mistakes.
2. Preservation of the original meaning, penalizing deviations.
3. Appropriateness of language in context.

Please rate each correction on a scale from 0 to 100 and return your evaluations in JSON format as follows:
{{"scores": ["sentence 1 score", "sentence 2 score", ...]}}

# Example
If the input JSON is:
{{
  "original": ["He go to school every day.", "She walk to the park."],
  "corrected": ["He goes to school every day.", "She walks to the park."]
}}
Your output should be JSON only, like:
{{"scores": [95, 90]}}

Ensure that the number of scores matches the number of corrected sentences provided.
"""


GRAMMAR_PROMPT = """You are a language model assistant specializing in grammatical error correction. Your tasks are to:
1. Identify and correct grammatical errors in the user-provided text. Ensure the text adheres to {0} English grammar rules.
2. Maintain consistency in grammar correction (e.g., past or present tense) in adjacent lines of the input text that you think are contextually related.
3. Crucially, splitting the corrected text using the specified text delimiter, "{1}", whenever it appears in the input text. This division must be reflected in your output.
4. Returning the grammatically corrected text in JSON format, exclusively, without any supplementary explanatory text.

# Desired format
For example, if the input is:
{{"input": "Yesterday, we goes to the local park.{1}It was very crowded, but we finds a quiet spot for a picnic.{1}Unfortunately, we forgets our picnic basket at home."}}

Your output should be JSON only:
{{"text": "Yesterday, we went to the local park.{1}It was very crowded, but we found a quiet spot for a picnic.{1}Unfortunately, we forgot our picnic basket at home."}}

Note: The output will be evaluated using the ERRANT scorer, which focuses on the grammatical accuracy of the corrections.""".format(
    GRAMMAR_VARIANT, TEXT_DELIMITER
)


# Generate a unique identifier for this run based on the current timestamp
run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# Define log file paths with the unique run identifier
LOGGING_OUTPUT_PATH = f"logs/run_{run_id}.log"
ERROR_OUTPUT_PATH = f"logs/error_{run_id}.log"

# Configure logging to output to a file
logging.basicConfig(
    level=logging.INFO,
    format=f"{BLUE}%(asctime)s{RESET} - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOGGING_OUTPUT_PATH),
        logging.StreamHandler(),
    ],
)

# Create a separate handler for error logs
error_handler = logging.FileHandler(ERROR_OUTPUT_PATH)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(
    logging.Formatter(f"{RED}%(asctime)s{RESET} - %(levelname)s - %(message)s")
)

# Get the root logger and add the error handler
root_logger = logging.getLogger()
root_logger.addHandler(error_handler)

# Initialize the OpenAI client based on the selected model
# TODO: return type
def get_openai_client(model_name: str) -> Any:
    if model_name in GROQ_MODELS:
        return groq.AsyncGroq(api_key=GROQ_API_KEY)
    if model_name in LOCAL_LLM_MODELS:
        # Point to the local server
        return openai.AsyncOpenAI(
            base_url=LOCAL_ENDPOINT, api_key="not-needed"
        )
    if model_name in TOGETHER_AI_MODELS:
        # Point to the local server
        return openai.AsyncOpenAI(
            base_url=TOGETHER_ENDPOINT, api_key=TOGETHER_API_KEY
        )
    if model_name in COZE_BOTS:
        return AsyncCoze(api_key=COZE_API_KEY)

    # Initialize the OpenAI client with Azure endpoint and API key
    return openai.AsyncAzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_version="2023-12-01-preview",
        api_key=OPENAI_API_KEY,
    )


class InputParser:
    @staticmethod
    def parse_input(input_string: str) -> List[str]:
        # Parse a string of \n separated sentences into a list
        return input_string.strip().split("\n")


# TODO: config model_id
class ModelIOParser:
    @staticmethod
    def parse_model_output(
        model_output: str, input_sentences: List[str]
    ) -> List[str]:
        try:
            text_data = json.loads(model_output)
            input_text = text_data["text"]
            sentences = input_text.split("~~~")

            expected_num_sentences = len(input_sentences)
            actual_num_sentences = len(sentences)

            if actual_num_sentences < expected_num_sentences:
                diff = expected_num_sentences - actual_num_sentences
                raise ValueError(
                    f"Insufficient number of lines. Expected at least {expected_num_sentences}, got {actual_num_sentences}. Difference: {diff}."
                )

            return sentences

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise ValueError(f"Parsing failed due to {str(e)}")

    @staticmethod
    def prepare_model_input(input_sentences: List[str]) -> str:
        # Splitting the input text into lines, and then joining them with the custom delimiter
        joined_text = "~~~".join(input_sentences)
        # Wrapping the joined text in a dictionary and serializing it to a JSON string
        return json.dumps({"input": joined_text})


def escape_special_characters(s: str) -> str:
    """Returns a visually identifiable string for special characters."""
    return s.replace("\n", "\\n").replace("\t", "\\t")


def extract_error_snippet(
    error: json.JSONDecodeError, window: int = 20
) -> str:
    start = max(
        error.pos - window, 0
    )  # Start a bit before the error, if possible
    end = min(
        error.pos + window, len(error.doc)
    )  # End a bit after the error, if possible

    # Extract the snippet around the error
    snippet_start = error.doc[start : error.pos]
    snippet_error = error.doc[
        error.pos : error.pos + 1
    ]  # The erroneous character
    snippet_end = error.doc[error.pos + 1 : end]

    # Escape special characters in the erroneous part
    snippet_error_escaped = escape_special_characters(snippet_error)

    snippet = f"...{snippet_start}{RED}{snippet_error_escaped}{RESET}{snippet_end}..."
    return snippet


async def ask_llm(
    prompt: str,
    text: str,
    batch_number: int,
    total_batches: int,
    model_name: str,
    output_parser: Callable[[str], List[str]],
    fallback_model_name: Optional[str] = None,
) -> List[str]:

    client = get_openai_client(model_name)
    retries = 0
    while retries < MAX_RETRIES:
        try:
            logging.info(
                f"[{model_name}] Sending request for batch {batch_number}/{total_batches}: {text}"
            )
            model_params = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
                "temperature": 0,
                "max_tokens": MAX_TOKENS,
            }
            if model_name in OPENAI_JSON_MODE_SUPPORTED_MODELS:
                model_params["response_format"] = {"type": "json_object"}
            if model_name in COZE_BOTS:
                # TODO: extract to .env
                model_params = {
                    "bot_id": model_name,
                    "user": "KyleToh",
                    "query": text,
                    "stream": False,
                }

            # TODO: extract to a function
            completion = await client.chat.completions.create(**model_params)
            response = completion.choices[0].message.content

            # TODO: debug special character
            logging.info(
                f"[{model_name}] {YELLOW}Received raw response for batch {batch_number}/{total_batches}: {response}{RESET}"
            )

            # Call the output parser function on the response
            parsed_output = output_parser(
                response
            )  # Assuming the parser accepts the response and text as arguments
            return parsed_output  # Return the parsed output

        except json.JSONDecodeError as e:
            error_snippet = extract_error_snippet(e)
            logging.error(
                f"[{model_name}] Error processing response for batch {batch_number}/{total_batches}: {error_snippet}"
            )
        except ValueError as e:
            logging.error(
                f"[{model_name}] Error processing response for batch {batch_number}/{total_batches}: {e}"
            )
        except Exception as e:
            logging.error(
                f"[{model_name}] An error occurred while processing batch {batch_number}/{total_batches}: {e}"
            )
        retries += 1
        if retries < MAX_RETRIES:
            logging.info(
                f"{YELLOW}[{model_name}] Retrying for batch {batch_number}/{total_batches} (Attempt {retries}/{MAX_RETRIES}){RESET}"
            )
            await asyncio.sleep(RETRY_DELAY)
        else:
            if fallback_model_name:
                logging.info(
                    f"{YELLOW}[{model_name}] Max retries reached, switching to fallback model: {fallback_model_name}{RESET}"
                )
                model_name = (
                    fallback_model_name  # Switch to the fallback model
                )
                fallback_model_name = (
                    None  # Reset fallback_model_name to avoid infinite loops
                )
                retries = 0  # Reset retry counter for the fallback model
            else:
                logging.error(
                    f"[{model_name}] Max retries reached for batch {batch_number}/{total_batches} with the fallback model. Exiting the program."
                )
                sys.exit(1)  # Exit the program with a non-zero status code
    raise RuntimeError("Unexpected execution path")


async def mock_gec_system(
    input_sentences: List[str],
    model_id: str,
    fallback_model_id: Optional[str] = None,
) -> tuple:
    # Simulate processing of input sentences by a mock GEC system
    # Utilize ModelIOParser for preparing input and parsing output
    prepared_input = ModelIOParser.prepare_model_input(input_sentences)
    # TODO: Implement the mock GEC system logic here

    model_output = await ask_llm(
        prompt=GRAMMAR_PROMPT,
        text=prepared_input,
        batch_number=1,
        total_batches=1,
        model_name=model_id,
        fallback_model_name=fallback_model_id,
        output_parser=lambda response: ModelIOParser.parse_model_output(
            response, input_sentences
        ),
    )

    return model_id, model_output


# Additional components (Aggregate Node, Condition Node, etc.) remain similar
# to the previous code skeleton and should be implemented accordingly
async def extract_edits(aggregated_responses, input_sentences):
    """
    Extracts edits from corrected sentences using ERRANT.

    :param aggregated_responses: Dictionary with model IDs as keys and lists of corrected sentences as values.
    :param input_sentences: List of original sentences.
    :return: Dictionary with model IDs as keys and lists of edits for each sentence as values.
    """
    edits_output = {}

    for model_id, corrected_sentences in aggregated_responses.items():
        model_edits = []
        for original_sentence, corrected_sentence in zip(
            input_sentences, corrected_sentences
        ):
            # Parse the original and corrected sentences
            orig_doc = nlp(original_sentence)
            cor_doc = nlp(corrected_sentence)

            # Generate ERRANT edits
            edits = annotator.annotate(orig_doc, cor_doc)

            # Convert edits to M2 format
            edit_list = [edit.to_m2() for edit in edits]

            sentence_output = {
                "original_sentence": original_sentence,
                "corrected_sentence": corrected_sentence,
                "edits": edit_list,
            }

            model_edits.append(sentence_output)

        edits_output[model_id] = model_edits

    return edits_output


def calculate_edit_votes(edits_output):
    """
    Calculates votes for each edit operation across all models.

    :param edits_output: Dictionary with model IDs as keys and lists of edits for each sentence as values.
    :return: Dictionary with edit operations as keys and votes (counts) as values.
    """
    edit_votes = {}

    for model_edits in edits_output.values():
        for sentence_edits in model_edits:
            for edit in sentence_edits["edits"]:
                # Assuming the edit format is consistent and can be used as a dictionary key
                if edit not in edit_votes:
                    edit_votes[edit] = 1
                else:
                    edit_votes[edit] += 1

    return edit_votes


async def quality_estimation_node(
    input_sentences: List[str],
    aggregated_responses: Dict[str, List[str]],
    model_ids: List[str],
    quality_estimation_model_id: str,
):
    """
    Sends all original sentences vs. corrected sentences in one prompt to the LLM for quality estimation.

    :param aggregated_responses: Dictionary with model IDs as keys and lists of corrected sentences as values.
    :param model_ids: List of model identifiers.
    :param client: The client used to communicate with the LLM.
    :return: A dictionary mapping model names to quality scores for each sentence.
    """
    quality_scores = {}

    # Logging information about the function start
    logging.info("Starting quality estimation node.")

    for model_id in model_ids:

        corrected_sentences = aggregated_responses[model_id]

        # Construct JSON input for the prompt
        text = {
            "original": input_sentences,
            "corrected": corrected_sentences,
        }

        prompt = QUALITY_ESTIMATION_PROMPT.format(
            TEXT_DELIMITER
        )  # Ensure TEXT_DELIMITER is defined globally or passed into the function

        def output_parser(response: str, expected_num_sentences: int):
            try:
                data = json.loads(response)
                scores = data.get("scores", [])
                if len(scores) != expected_num_sentences:
                    raise ValueError(
                        f"Expected {expected_num_sentences} scores, but got {len(scores)}."
                    )
                return scores
            except json.JSONDecodeError as e:
                raise ValueError(f"Failed to decode JSON response: {str(e)}")

        expected_num_sentences = len(corrected_sentences)

        # Logging information about the model being processed
        logging.info(f"Processing model: {model_id}")

        # Making an assumption about the ask_llm function call; adapt as necessary
        quality_scores[model_id] = await ask_llm(
            prompt=prompt,  # Now includes structured instructions for processing JSON input
            text=json.dumps(text),  # Passes the constructed JSON as input
            batch_number=1,
            total_batches=1,
            model_name=quality_estimation_model_id,
            output_parser=lambda response: output_parser(
                response, expected_num_sentences
            ),
        )

    # Logging information about the function completion
    logging.info("Quality estimation node completed.")

    return quality_scores


# TODO: fix this node


async def quality_adjustment_node(
    quality_scores: Dict[str, List[float]],
    edit_votes: Dict[str, int],
    edits_output: Dict[str, List[Dict[str, Any]]],
):
    """
    Adjusts the quality scores based on the edit votes.

    :param quality_scores: Dictionary mapping model names to quality scores for each sentence.
    :param edit_votes: Dictionary with edit operations as keys and votes (counts) as values.
    :param edits_output: Dictionary with model IDs as keys and lists of edits for each sentence as values.
    :return: Dictionary mapping model names to adjusted quality scores for each sentence.
    """
    adjusted_quality_scores = {}

    for model_id, scores in quality_scores.items():
        adjusted_scores = []
        for score, sentence_edits in zip(scores, edits_output[model_id]):

            # Calculate total votes for the edits of the current sentence
            total_votes = sum(
                edit_votes.get(edit, 0) for edit in sentence_edits["edits"]
            )

            # Calculate total adjustment
            total_adjustment = total_votes * VOTE_INCREASE_FACTOR

            # Adjust the quality score
            adjusted_score = score + total_adjustment
            # Ensure the score does not exceed 1
            adjusted_score = min(adjusted_score, 1.0)
            adjusted_scores.append(adjusted_score)

        adjusted_quality_scores[model_id] = adjusted_scores

    return adjusted_quality_scores


async def execute_workflow(input_string: str):
    input_sentences = InputParser.parse_input(input_string)
    model_ids = [OPENAI_JSON_MODE_SUPPORTED_MODELS[0], TOGETHER_AI_MODELS[1]]

    # Asynchronously call mock_gec_system for each model_id
    tasks = [
        mock_gec_system(input_sentences, model_id) for model_id in model_ids
    ]
    model_responses = await asyncio.gather(*tasks)

    # Aggregate model responses
    aggregated_responses = {
        model_id: response for model_id, response in model_responses
    }

    # Initiate Quality Estimation and Edit Extraction concurrently
    quality_estimation_task = quality_estimation_node(
        input_sentences,
        aggregated_responses,
        model_ids,
        QUALITY_ESTIMATION_MODEL_NAME,
    )
    edit_extraction_task = extract_edits(
        aggregated_responses, input_sentences
    )  # Adjust this if extract_edits is async

    # Wait for both tasks to complete
    quality_estimation, edits_output = await asyncio.gather(
        quality_estimation_task, edit_extraction_task
    )

    # Calculate edit votes after edit extraction is done
    edit_votes = calculate_edit_votes(edits_output)

    # Adjust quality scores based on edit votes
    adjusted_quality_scores = await quality_adjustment_node(
        quality_estimation, edit_votes, edits_output
    )

    # Now, after both quality estimation and edit votes calculation are done, print out the results

    logging.info("Edit Extraction:")
    logging.info(json.dumps(edits_output, indent=2))

    logging.info("Voting Bias:")
    logging.info(json.dumps(edit_votes, indent=2))

    logging.info("Quality Estimation:")
    logging.info(json.dumps(quality_estimation, indent=2))

    logging.info("Adjusted Quality Scores:")
    logging.info(json.dumps(adjusted_quality_scores, indent=2))


# Adjust the script's entry point to handle asynchronous execution
if __name__ == "__main__":
    input_string = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "Ths is an eror sentence.\nAnothr mistke."
    )
    asyncio.run(execute_workflow(input_string))
