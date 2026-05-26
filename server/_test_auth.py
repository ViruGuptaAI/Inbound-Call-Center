"""Quick test: verify DefaultAzureCredential gets a token from the correct tenant."""
import asyncio, json, base64
from azure.identity.aio import DefaultAzureCredential

async def main():
    async with DefaultAzureCredential(exclude_developer_cli_credential=True) as cred:
        token = await cred.get_token("https://cognitiveservices.azure.com/.default")
        # Decode JWT payload (no signature verification needed for inspection)
        payload = token.token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)  # pad base64
        claims = json.loads(base64.b64decode(payload))
        print(f"tenant (tid): {claims.get('tid')}")
        print(f"audience (aud): {claims.get('aud')}")
        print(f"issuer (iss): {claims.get('iss')}")
        print("Token OK")

asyncio.run(main())
