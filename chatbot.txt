Build a lightweight inference logging and ingestion system for an LLM application.
1. Chatbot Application
Build a simple chatbot using any foundation model API.
Examples:
GPT-4.1
Claude Sonnet
Gemini
DeepSeek
Grok
any equivalent model
The chatbot should:
support multi-turn conversations
maintain short conversational context
expose a simple UI

2. Lightweight SDK / Wrapper
Create a lightweight SDK, middleware, or wrapper around your LLM calls that captures inference metadata.
Examples of metadata:
model
provider
latency
token usage
timestamps
request status/errors
conversation/session ID
input/output previews
The SDK should send logs to an ingestion endpoint in near real time. Implementation details are flexible.

3. Ingestion Pipeline
Build an ingestion service/API that:
receives logs from the SDK
validates/parses payloads
extracts useful metadata
stores processed data in a database

4. Database Storage
Store:
chat messages
inference logs
extracted metadata
We care about sensible schema design and practical tradeoffs.

Deliverables
1. GitHub Repository
Complete source code.
2. README
Include:
setup instructions
architecture overview
schema design decisions
tradeoffs made
what you would improve with more time
3. Architecture Notes
Briefly explain:
ingestion flow
logging strategy
scaling considerations
failure handling assumptions
4. Demo
Hosted link, screenshots, or Loom video.

Bonus 
You will be given a guaranteed interview if you are able to complete the following task.
Multi-provider support
Streaming Responses
Latency + Throughput + Errors dashboards
Docker Compose one-command setup
Event based architecture
PII redaction
Deploy application on self hosted k8s
Frontend
The UI allows following:
Cancel a conversation
List conversations
Resume a conversation

Submission
Please send:
GitHub repo
architecture notes
demo link (optional)
to: work@ollive.ai
Looking forward to seeing what you build 🚀

