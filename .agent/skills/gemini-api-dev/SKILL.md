---
name: gemini-api-dev
description: Use when building or debugging Gemini API integrations, choosing Gemini models, working with multimodal generation, or needing current official Gemini SDK and documentation references.
---

# Gemini API Development Skill

## Overview

Gemini model names, SDKs, and preview capabilities change often. Treat official Google documentation as the source of truth before hard-coding model IDs, request fields, or library guidance.

## Rules

- Prefer the official GenAI SDKs:
  - Python: `google-genai`
  - JavaScript/TypeScript: `@google/genai`
  - Go: `google.golang.org/genai`
  - Java: `com.google.genai:google-genai`
- Avoid legacy SDKs unless the codebase is explicitly pinned to them:
  - `google-generativeai`
  - `@google/generative-ai`
- Re-check current model names in the official docs before adding or changing them in code or skills.

## Source Of Truth

- Docs index: [llms.txt](https://ai.google.dev/gemini-api/docs/llms.txt)
- SDK docs: [libraries](https://ai.google.dev/gemini-api/docs/libraries)
- Model catalog: [models](https://ai.google.dev/gemini-api/docs/models)
- Image generation: [image-generation](https://ai.google.dev/gemini-api/docs/image-generation)
- Function calling: [function-calling](https://ai.google.dev/gemini-api/docs/function-calling)
- Structured outputs: [structured-output](https://ai.google.dev/gemini-api/docs/structured-output)
- Migration guide: [migrate](https://ai.google.dev/gemini-api/docs/migrate)

## Working Pattern

1. Verify the current official model and SDK docs.
2. Match examples to the target language and API version.
3. Use official SDK calls unless the repository already chose raw REST.
4. If a model is preview or image-specific, avoid assuming it is stable across time.
5. When documenting or encoding a model ID in a skill, say that it was verified against official docs.

## Minimal Examples

```python
from google import genai

client = genai.Client()
response = client.models.generate_content(
    model="gemini-3-flash-preview",
    contents="Explain quantum computing"
)
print(response.text)
```

```typescript
import { GoogleGenAI } from "@google/genai";

const ai = new GoogleGenAI({});
const response = await ai.models.generateContent({
  model: "gemini-3-flash-preview",
  contents: "Explain quantum computing"
});
console.log(response.text);
```

## Notes

- If the request is about image generation, verify the current image model example in the official image-generation docs before hard-coding it.
- If the repository already has a working Gemini integration, follow that repository's existing auth, error handling, and API version choices.
