"""
Test both Gemini keys independently:
Key 1 (GOOGLE_API_KEY) via Google ADK
Key 2 (GEMINI_API_KEY) via CrewAI LLM
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.core.config import get_settings

settings = get_settings()
key1 = getattr(settings, "google_api_key", "") or os.environ.get("GOOGLE_API_KEY", "")
key2 = getattr(settings, "gemini_api_key", "") or os.environ.get("GEMINI_API_KEY", "")

masked1 = f"{key1[:7]}...[REDACTED]" if key1 else "NOT SET"
masked2 = f"{key2[:7]}...[REDACTED]" if key2 else "NOT SET"

print("=" * 60)
print(f"Key 1 for Google ADK (GOOGLE_API_KEY): {masked1}")
print(f"Key 2 for CrewAI    (GEMINI_API_KEY): {masked2}")
print(f"Are keys distinct? {key1 != key2}")
print("=" * 60)

# 1. Test Google ADK with Key 1
print("\n--- 1. Testing Google ADK with Key 1 (GOOGLE_API_KEY) ---")
os.environ["GOOGLE_API_KEY"] = key1
os.environ["GEMINI_API_KEY"] = key1

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
import google.genai.types as _gt

async def test_adk():
    session_service = InMemorySessionService()
    session_id = "adk-test-session-1"
    
    res = session_service.create_session(app_name="adk_test", user_id="user1", session_id=session_id)
    if asyncio.iscoroutine(res):
        await res

    agent = LlmAgent(
        model="gemini-3.5-flash-lite",
        name="adk_test_agent",
        instruction="Reply with only the exact word: ADK_SUCCESS"
    )
    runner = Runner(agent=agent, app_name="adk_test", session_service=session_service)
    msg = _gt.Content(role="user", parts=[_gt.Part(text="Health check")])
    
    raw = ""
    async for event in runner.run_async(user_id="user1", session_id=session_id, new_message=msg):
        is_final = getattr(event, "is_final_response", lambda: False)() or getattr(event, "is_last_chunk", False)
        if is_final:
            content = getattr(event, "content", None)
            if content and getattr(content, "parts", None):
                raw = getattr(content.parts[0], "text", "") or ""
            break
    return raw.strip()

try:
    adk_out = asyncio.run(test_adk())
    print(f"ADK Execution SUCCESS! Output: '{adk_out}'")
except Exception as e:
    print(f"ADK Execution FAILED: {type(e).__name__}: {e}")

# 2. Test CrewAI with Key 2
print("\n--- 2. Testing CrewAI Native LLM with Key 2 (GEMINI_API_KEY) ---")
from crewai import LLM

try:
    crew_llm = LLM(
        model="gemini/gemini-3.5-flash-lite",
        api_key=key2
    )
    crew_out = crew_llm.call(messages=[{"role": "user", "content": "Reply with only the exact word: CREW_SUCCESS"}])
    print(f"CrewAI LLM Execution SUCCESS! Output: '{crew_out.strip()}'")
except Exception as e:
    print(f"CrewAI LLM Execution FAILED: {type(e).__name__}: {e}")

print("\n" + "=" * 60)
print("Independent provider tests completed.")

