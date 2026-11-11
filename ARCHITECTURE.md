# Media Agent Architecture

## Goal

Turn one natural-language request into a researched media recommendation or an
authorized movie/TV download workflow.

## Runtime flow

```text
User prompt
    |
    v
CLI (run_agent.py)
    |
    v
LangChain create_agent (LangGraph runtime + memory)
    |
    +--> Gemini 3.5 Flash-Lite: plans and selects tools
    |
    +--> web_search: current web results
    +--> discover_similar_titles: candidate connections to a seed title
    +--> imdb_lookup / filter_imdb_titles: web title resolution + official IMDb ratings dataset
    +--> legal_watch_options: current official streaming/rental discovery
    +--> download_tv_series: existing season ZIP workflow
    +--> download_movie: Telegram movie delivery workflow
    +--> download_sinhala_subtitle_file: Sinhala subtitle search and local save
    |
    v
Grounded response, recommendation list, or local output path
```

## Security and safety boundaries

- The Gemini key is encrypted using Windows DPAPI and bound to the current
  Windows user. Web search and IMDb checks require no additional API key.
- IMDb's public ratings dataset is cached as a local SQLite index and refreshed
  after 24 hours.
- The existing Telegram API credential and session are reused; they are not
  copied into this project.
- Download tools require an explicit `authorized=true` argument. The agent is
  instructed to request confirmation that the user owns the media, has a
  licence, or the content is lawfully downloadable before invoking them.
- Search and ratings are read-only. Download tools are the only tools that
  write media files.
- Telegram transfers use 512 KB chunks. TV seasons download three episodes at
  a time; a single movie is split into three concurrent byte ranges. Transfer
  concurrency is capped at three workers.
- Movie and TV download tools create `downloads/<Title>/`, invoke
  `@Sinhala_Subtitles_Bot`, and place all matching subtitle suggestions beside
  the media output.

## Extension points

Add a function decorated with `@tool` in `media_agent/tools.py`, then register
it in `ALL_TOOLS`. Keep each tool narrow, typed, and explicit about side effects.

