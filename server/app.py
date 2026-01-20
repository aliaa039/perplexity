from typing import TypedDict, Annotated, Optional
from uuid import uuid4
import json
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from langgraph.graph import add_messages, StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessageChunk, ToolMessage
from langchain_community.tools.tavily_search import TavilySearchResults

# ------------------------------------------------------------------
# ENV
# ------------------------------------------------------------------
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing")

# ------------------------------------------------------------------
# Memory
# ------------------------------------------------------------------
memory = MemorySaver()

# ------------------------------------------------------------------
# State
# ------------------------------------------------------------------
class State(TypedDict):
    messages: Annotated[list, add_messages]

# ------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------
search_tool = TavilySearchResults(
    max_results=4,
    api_key=TAVILY_API_KEY,
)

tools = [search_tool]

# ------------------------------------------------------------------
# LLM (Groq – correct model)
# ------------------------------------------------------------------
llm = ChatOpenAI(
    model="llama-3.1-8b-instant",  # ✅ correct Groq model
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY,
    temperature=0.2,
)

llm_with_tools = llm.bind_tools(tools)

# ------------------------------------------------------------------
# Nodes
# ------------------------------------------------------------------
async def model(state: State):
    result = await llm_with_tools.ainvoke(state["messages"])
    return {"messages": [result]}

async def tools_router(state: State):
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tool_node"
    return END

async def tool_node(state: State):
    tool_calls = state["messages"][-1].tool_calls
    tool_messages = []

    for call in tool_calls:
        if call["name"] == "tavily_search_results_json":
            result = await search_tool.ainvoke(call["args"])
            tool_messages.append(
                ToolMessage(
                    content=json.dumps(result),
                    tool_call_id=call["id"],
                    name=call["name"],
                )
            )

    return {"messages": tool_messages}

# ------------------------------------------------------------------
# Graph
# ------------------------------------------------------------------
graph_builder = StateGraph(State)

graph_builder.add_node("model", model)
graph_builder.add_node("tool_node", tool_node)
graph_builder.set_entry_point("model")
graph_builder.add_conditional_edges("model", tools_router)
graph_builder.add_edge("tool_node", "model")

graph = graph_builder.compile(checkpointer=memory)

# ------------------------------------------------------------------
# FastAPI
# ------------------------------------------------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def serialise_chunk(chunk: AIMessageChunk) -> str:
    return chunk.content or ""

# ------------------------------------------------------------------
# SSE Generator
# ------------------------------------------------------------------
async def generate_chat_responses(
    message: str,
    checkpoint_id: Optional[str] = None,
):
    if checkpoint_id is None:
        checkpoint_id = str(uuid4())
        yield "data: " + json.dumps({
            "type": "checkpoint",
            "checkpoint_id": checkpoint_id,
        }) + "\n\n"

    events = graph.astream_events(
        {"messages": [HumanMessage(content=message)]},
        version="v2",
        config={"configurable": {"thread_id": checkpoint_id}},
    )

    async for event in events:
        etype = event["event"]

        if etype == "on_chat_model_stream":
            chunk = serialise_chunk(event["data"]["chunk"])
            yield "data: " + json.dumps({
                "type": "content",
                "content": chunk,
            }) + "\n\n"

        elif etype == "on_tool_end" and event["name"] == "tavily_search_results_json":
            output = event["data"]["output"]
            urls = [item["url"] for item in output if isinstance(item, dict) and "url" in item]
            yield "data: " + json.dumps({
                "type": "search_results",
                "urls": urls,
            }) + "\n\n"

    yield "data: " + json.dumps({"type": "end"}) + "\n\n"

# ------------------------------------------------------------------
# Endpoint (Query params – correct for SSE)
# ------------------------------------------------------------------
@app.get("/chat_stream")
async def chat_stream(
    message: str = Query(...),
    checkpoint_id: Optional[str] = Query(None),
):
    return StreamingResponse(
        generate_chat_responses(message, checkpoint_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
