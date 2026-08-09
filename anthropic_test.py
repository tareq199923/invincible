from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:8000",
    api_key="gw-secret-dev-token",
)

response = client.messages.create(
    model="gemini-2.5-flash",
    max_tokens=128,
    messages=[
        {
            "role": "user",
            "content": "Say hello from the Anthropic SDK!"
        }
    ]
)

print(response.content[0].text)
