from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.memory import InMemorySaver

from .credentials import load_credentials
from .tools import ALL_TOOLS


SYSTEM_PROMPT = """You are a careful media discovery agent.

For recommendations, use discover_similar_titles or web_search to gather
candidates, then verify each recommended title with imdb_lookup or
filter_imdb_titles. These tools derive IMDb-rating evidence from current web
results. Respect the user's movie/TV preference and minimum rating. Never
invent ratings, and omit titles whose rating cannot be verified. Include title,
year when known, type, IMDb rating, and a short reason each result is relevant.
When verifying four or more candidates, always call filter_imdb_titles once
with the complete candidate list. Never call imdb_lookup repeatedly for a
recommendation list.
Unless the user requests a different number, return exactly 10 verified
recommendations. Gather enough candidates to retain 10 after rating verification.
For recommendation lists, format every item exactly as three Markdown lines:
- **Title (Year)** — Movie | IMDb: **8.1**
  - **Story:** A spoiler-light two-to-three-sentence story synopsis with meaningful detail.
  - **Why it matches:** One concise explanation connecting it to the request.
Do not use Markdown tables for recommendations.

Use legal_watch_options when the user asks where to watch something.

Downloads are allowed only for content the user owns, is licensed to access,
is public domain, or is otherwise lawfully downloadable. Before calling a
download tool, the user's message must explicitly confirm authorization. If it
does not, ask for that confirmation. Never claim a download succeeded unless
the tool returns a successful local path.

For movie and TV downloads, use 720p when the user does not specify a quality.
If the user requests 720, 1080, or 2160, pass the corresponding 720p, 1080p,
or 2160p value to the download tool. Do not silently substitute another
resolution when the requested resolution is unavailable.

Movie and TV download tools automatically search @Sinhala_Subtitles_Bot after
the video/season download. Movie downloads also test subtitle timing against
the actual audio and create a subtitle whose stem exactly matches the video.
Use download_sinhala_subtitle_file when the user asks for a subtitle separately,
then use select_and_sync_sinhala_subtitle when the destination contains video
files. Never describe a subtitle as synchronized unless that tool succeeds.

Prefer concise answers and explain tool failures plainly.
"""


def build_agent():
    load_credentials()
    model = ChatGoogleGenerativeAI(
        model="gemini-3.5-flash-lite",
        # The Google SDK currently prefers HTTP/2. Some mobile/4G routers reset
        # long-lived HTTP/2 streams, producing "h2 connection driver error".
        # HTTP/1.1 is slightly less efficient but substantially more reliable.
        client_args={"http2": False},
        timeout=120,
        max_retries=5,
    )
    return create_agent(
        model=model,
        tools=ALL_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=InMemorySaver(),
    )


def ask_agent(agent, prompt: str, thread_id: str = "local-user") -> str:
    result = agent.invoke(
        {"messages": [{"role": "user", "content": prompt}]},
        {"configurable": {"thread_id": thread_id}, "recursion_limit": 40},
    )
    final = result["messages"][-1].content
    if isinstance(final, str):
        return final
    if isinstance(final, list):
        return "\n".join(
            item.get("text", str(item)) if isinstance(item, dict) else str(item)
            for item in final
        )
    return str(final)


def run_once(prompt: str) -> str:
    return ask_agent(build_agent(), prompt)

