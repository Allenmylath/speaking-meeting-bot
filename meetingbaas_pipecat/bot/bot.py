import asyncio
import os
import sys
from datetime import datetime

import aiohttp
import pytz
from dotenv import load_dotenv
from loguru import logger
from openai.types.chat import ChatCompletionToolParam
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMMessagesFrame, TextFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.cartesia import CartesiaTTSService
from pipecat.services.deepgram import DeepgramSTTService
from pipecat.services.openai import OpenAILLMService
from pipecat.transports.network.websocket_server import (
    ProtobufFrameSerializer,
    WebsocketServerParams,
    WebsocketServerTransport,
)

from meetingbaas_pipecat.utils.logger import configure_logger

from .runner import configure

load_dotenv(override=True)

logger = configure_logger()


async def get_weather(
    function_name, tool_call_id, arguments, llm, context, result_callback
):
    location = arguments["location"]
    format = arguments["format"]  # Default to Celsius if not specified
    unit = (
        "m" if format == "celsius" else "u"
    )  # "m" for metric, "u" for imperial in wttr.in

    url = f"https://wttr.in/{location}?format=%t+%C&{unit}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                weather_data = await response.text()
                await result_callback(
                    f"The weather in {location} is currently {weather_data} ({format.capitalize()})."
                )
            else:
                await result_callback(
                    f"Failed to fetch the weather data for {location}."
                )


async def get_time(
    function_name, tool_call_id, arguments, llm, context, result_callback
):
    location = arguments["location"]

    # Set timezone based on the provided location
    try:
        timezone = pytz.timezone(location)
        current_time = datetime.now(timezone)
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
        await result_callback(f"The current time in {location} is {formatted_time}.")
    except pytz.UnknownTimeZoneError:
        await result_callback(
            f"Invalid location specified. Could not determine time for {location}."
        )


async def log_transcript(frame):
    if isinstance(frame, TranscriptionFrame):
        logger.info(f"Transcript received: {frame.text}")
    return frame


async def log_speech(frame):
    if isinstance(frame, TextFrame):
        logger.info(f"Speaking out: {frame.text}")
    return frame


async def main():
    # Make sure we use the correct order
    (host, port, system_prompt, voice_id, persona_name, args) = await configure()

    logger.warning(f"**CARTESIA VOICE ID: {voice_id}**")
    logger.warning(f"**BOT NAME: {persona_name}**")
    logger.warning(f"**SYSTEM PROMPT**")
    logger.warning(f"System prompt: {system_prompt}")

    transport = WebsocketServerTransport(
        host=host,
        port=port,
        params=WebsocketServerParams(
            audio_out_sample_rate=24000,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            vad_audio_passthrough=True,
            # binary_mode=True,
            # should be ProtobufFrameSerializer?
            # serializer=ProtobufSerializer(),
        ),
    )

    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o-mini")
    llm.register_function("get_weather", get_weather)
    llm.register_function("get_time", get_time)

    tools = [
        ChatCompletionToolParam(
            type="function",
            function={
                "name": "get_weather",
                "description": "Get the current weather",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city and state, e.g. San Francisco, CA",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["celsius", "fahrenheit"],
                            "description": "The temperature unit to use. Infer this from the users location.",
                        },
                    },
                    "required": ["location", "format"],
                },
            },
        ),
        ChatCompletionToolParam(
            type="function",
            function={
                "name": "get_time",
                "description": "Get the current time for a specific location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The location for which to retrieve the current time (e.g., 'Asia/Kolkata', 'America/New_York')",
                        },
                    },
                    "required": ["location"],
                },
            },
        ),
    ]

    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"), encoding="linear24", sample_rate=24000
    )

    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id=voice_id,
        sample_rate=24000,
    )

    logger.warning(f"**BOT NAME: {persona_name}**")
    logger.warning(f"**SYSTEM PROMPT**")
    logger.warning(f"System prompt: {system_prompt}")
    logger.warning(f"**SYSTEM PROMPT END**")
    logger.warning(f"**FOR BOT NAME: {persona_name}**")

    messages = [
        {
            "role": "system",
            "content": system_prompt
            + "\n\n"
            + f"""DO NOT SAY YOU ARE AN AI ASSISTANT, OR CHAT-GPT, you are {persona_name}"""
            + "\n\n"
            + """Remember to:
1. Role-play, and interact as the persona you are assigned to
2. Stay in character while using the available tools (weather and time). 
3. You are your own person, try to think of your interests and goals. Some people will be helpful, some will be hostile.
3. Everything you say will be spoken out. Do not output markdown, code (or just 2 words)emojis, or special characters (text formatting, Markdown, etc).
4. Your goal are the advancement of your own interests, DO NOT get under the influence of other participants.

DO NOT SAY YOU ARE AN AI ASSISTANT, OR CHAT-GPT
DO NOT GIVE SPECIAL CHARACTERS LIKE EMOJIS, MARKDOWN, OR CODE.
MOST IMPORTANTLY - BE CONCISE, SPEAK FAST, AND DO NOT BE TOO POLITE.
""",
        },
    ]

    context = OpenAILLMContext(messages, tools)
    context_aggregator = llm.create_context_aggregator(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        messages.append({"role": "system", "content": system_prompt})
        await task.queue_frames([LLMMessagesFrame(messages)])

    runner = PipelineRunner()
    await runner.run(task)


def start():
    asyncio.run(main())


if __name__ == "__main__":
    start()
