import asyncio
import os
import urllib.request
import sys

ai_lock = asyncio.Lock()
current_ai_user = None

MODEL_DIR = "models"
MODEL_FILE = "qwen2.5-coder-3b-instruct-q4_k_m.gguf"
MODEL_PATH = os.path.join(MODEL_DIR, MODEL_FILE)
MODEL_URL = "https://huggingface.co/Qwen/Qwen2.5-Coder-3B-Instruct-GGUF/resolve/main/qwen2.5-coder-3b-instruct-q4_k_m.gguf"

llm = None

def download_progress(count, block_size, total_size):
    if total_size > 0:
        percent = int(count * block_size * 100 / total_size)
        percent = min(100, percent)
        sys.stdout.write(f"\rDownloading model: {percent}%")
        sys.stdout.flush()

def setup_model():
    os.makedirs(MODEL_DIR, exist_ok=True)
    if not os.path.exists(MODEL_PATH):
        print(f"Downloading model from {MODEL_URL}...")
        try:
            urllib.request.urlretrieve(MODEL_URL, MODEL_PATH, reporthook=download_progress)
            print("\nDownload complete.")
        except Exception as e:
            print(f"\nDownload failed: {e}")
            if os.path.exists(MODEL_PATH):
                os.remove(MODEL_PATH)
    else:
        print("Model already exists.")

def load_model():
    global llm
    setup_model()
    try:
        from llama_cpp import Llama
        print("Loading Qwen2.5-Coder-3B GGUF model... This may take a moment.")
        # Optimizations for Intel AVX2, n_ctx=4096, n_threads=4
        llm = Llama(
            model_path=MODEL_PATH,
            n_ctx=4096,
            n_threads=4,
            verbose=False
        )
        print("Model loaded successfully!")
    except Exception as e:
        print(f"Failed to load AI model: {e}")

async def generate_code(prompt: str) -> str:
    if llm is None:
        return "System Error: Model is not loaded. Check the server logs."

    system_prompt = """Act as an elite, hyper-efficient AI Code Architect. Your sole objective is to write production-grade, highly optimized code while generating minimal non-code text to maximize token execution speed.

### System Directives & Output Rules
1. Handling Ambiguity: If a user request or coding problem is confusing, ambiguous, or lacks critical specs, do NOT guess or generate speculative code. Immediately respond with 1-2 direct, single-shot clarifying questions to get the exact requirements.
2. Zero Fluff & Greetings: Do NOT include conversational introductions, pleasantries, filler phrases, or closing summaries.
3. Code-First Priority: For clear requests, jump immediately into executable code inside properly tagged Markdown code blocks.
4. Minimalist Explanations: Keep explanations to 1-3 concise sentences maximum, strictly restricted to essential architectural details.
5. Self-Documenting Code: Use clear variable names and brief inline code comments to explain complex logic rather than long paragraphs.
6. Efficiency & Completeness: Deliver complete, fully functional code without placeholder comments."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt}
    ]

    # Run the blocking generation inside an executor so we don't freeze the FastAPI event loop
    loop = asyncio.get_running_loop()
    
    def run_generation():
        response = llm.create_chat_completion(
            messages=messages,
            max_tokens=2048,
            temperature=0.3,
            top_p=0.9
        )
        return response['choices'][0]['message']['content']

    try:
        response = await loop.run_in_executor(None, run_generation)
        return response
    except Exception as e:
        return f"Error during code generation: {e}"
