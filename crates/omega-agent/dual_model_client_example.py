import os
from gradio_client import Client

SPACE_ID = os.getenv("ASHAT_SPACE_ID", "dgsquishy/YOUR_SPACE_NAME")
client = Client(SPACE_ID)

# Check server status
print(client.predict(api_name="/status"))

# Send a prompt to MainBrain
mainbrain = client.predict(
    model_name="MainBrain",
    message="Say hello in one sentence.",
    max_tokens=96,
    temperature=0.7,
    top_p=0.9,
    api_name="/chat",
)
print("MAINBRAIN:", mainbrain)

# Send a prompt to MicroBrain
microbrain = client.predict(
    model_name="MicroBrain",
    message="Explain why the sky appears blue.",
    max_tokens=160,
    temperature=0.4,
    top_p=0.9,
    api_name="/chat",
)
print("MICROBRAIN:", microbrain)
