# ================================================================
# FILE EXECUTION FLOW
# ================================================================
#
# [ START ]
#     |
#     v
# +----------------------------------------------+
# | __init__()                                   |
# | * validate API key and init Gemini client    |
# |----> getenv()                                |
# |        * load GEMINI_API_KEY from env        |
# |----> <genai.Client> -> __init__()            |
# |        * create Google GenAI client          |
# +----------------------------------------------+
#     |
#     v
# +----------------------------------------------+
# | generate_content()                           |
# | * call Gemini API with prompt                |
# |----> <genai.Client> -> generate_content()    |
# |        * Gemini inference call               |
# +----------------------------------------------+
#     |
#     v
# [ END ]
#
# ================================================================

import os

from dotenv import load_dotenv
import google.genai as genai

load_dotenv()


class GeminiResponder:

    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("VITE_MNI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY not found in .env — "
                "add it or set VITE_MNI_API_KEY as a fallback."
            )

        self.client   = genai.Client(api_key=api_key)
        self.model_id = "gemini-2.5-flash"
