# Context Observability Findings

Date: 2026-04-04

## Goal

This note summarizes the current `/context` observation approach, the problems found during implementation and verification, and the solution paths that look viable after checking Gemini official docs, the current `google-genai` SDK behavior, and direct REST calls.

## Current Implementation

The current `/context` implementation is split across:

- [src/myopenclaw/runs/context_usage.py](/Users/ssunxie/code/myopenclaw/src/myopenclaw/runs/context_usage.py)
- [src/myopenclaw/providers/gemini.py](/Users/ssunxie/code/myopenclaw/src/myopenclaw/providers/gemini.py)
- [src/myopenclaw/cli/context_renderer.py](/Users/ssunxie/code/myopenclaw/src/myopenclaw/cli/context_renderer.py)

The current snapshot model is:

1. `total_tokens`
   Derived from the latest persisted assistant metadata in session history.
   Current code uses `input_tokens + output_tokens` when both exist, otherwise falls back to `metadata.total_tokens`.

2. `system_tokens`
   Estimated by sending the base system instruction text through a provider token estimator.

3. `skills_tokens`
   Estimated by sending the skills guidance block plus the formatted skills catalog through a provider token estimator.

4. `tools_tokens`
   Estimated by sending tool declarations through a provider token estimator.

5. `messages_tokens`
   Computed as a residual:

```text
messages = total - system - skills - tools
```

6. `free_tokens`
   Computed as:

```text
free = max_input_tokens - total
```

## Problems Found

### 1. The residual can go negative

Observed example:

- `Total = 1507`
- `System = 90`
- `Skills = 331`
- `Tools = 1102`
- `Messages = -16`

This proves the four categories are not being measured with the same accounting boundary.

### 2. `input + output` is not a safe definition of "current resident context"

The initial working assumption was:

```text
one turn's input_tokens + output_tokens ~= the next turn's resident context
```

After checking Gemini official docs, this is not a reliable formula.

What is actually sent on the next turn is the next prompt payload reconstructed from conversation history, not the previous turn's total billable usage number.

Relevant distinctions:

- `promptTokenCount` is the prompt size for one generate request
- `candidatesTokenCount` is generated output
- `totalTokenCount` is the total usage for that request, not a durable "resident context" value

### 3. `count_tokens(contents=...)` is not guaranteed to be identical to real prompt usage

Gemini official docs show examples where:

- `countTokens(...contents...)`
- and `generateContent(...).usageMetadata.promptTokenCount`

are very close, but not always identical.

That means we cannot treat these paths as a mathematically exact identity in every case.

### 4. Python SDK support is narrower than the REST API support

As of local verification on 2026-04-04:

- installed SDK before upgrade: `google-genai 1.68.0`
- installed SDK after upgrade: `google-genai 1.70.0`

In both versions, the Gemini Developer API path in the Python SDK still rejects:

- `CountTokensConfig(system_instruction=...)`
- `CountTokensConfig(tools=...)`

The rejection is explicit in the installed SDK source:

- [models.py](/Users/ssunxie/code/myopenclaw/.venv/lib/python3.12/site-packages/google/genai/models.py#L337)

The current SDK behavior for Gemini Developer API is effectively:

- `contents` is supported
- `system_instruction` is rejected
- `tools` is rejected

This remains true after upgrading the local environment to `google-genai 1.70.0`.

### 5. The current Gemini estimator uses a lossy fallback for tools

Because the Python SDK path rejects `tools` in `count_tokens`, the current code falls back to:

- serialize tool declarations to JSON
- send that JSON string as `contents`

This is not the same thing as counting the real request shape that Gemini receives during `generate_content`.

That fallback is good enough for rough visibility, but not good enough for residual accounting.

## What Was Verified

### A. Gemini chat is stateless at the API level

Gemini official docs state that for multi-turn conversations, full conversation history is sent to the model on follow-up turns.

Implication:

- the next turn prompt must be reconstructed
- the current context should be modeled as "what the next request will send"
- not "the previous request's total usage"

### B. REST `countTokens(generateContentRequest=...)` works for `systemInstruction`

Direct REST test against Gemini Developer API succeeded when the payload used:

- `generateContentRequest.model`
- `generateContentRequest.systemInstruction`
- `generateContentRequest.contents`

Result:

- `countTokens.totalTokens = 6`
- matching `generateContent.usageMetadata.promptTokenCount = 6`

### C. REST `countTokens(generateContentRequest=...)` works for `tools`

Direct REST test against Gemini Developer API also succeeded when the payload used:

- `generateContentRequest.model`
- `generateContentRequest.tools`
- `generateContentRequest.contents`

Result:

- `countTokens.totalTokens = 34`
- matching `generateContent.usageMetadata.promptTokenCount = 34`

This is the most important technical finding in this investigation:

The REST API supports the request shape we need, while the current Python SDK Gemini path does not.

### D. `countTokens` is not billed and does not consume inference quota

Gemini billing docs state that requests to the token counting API are:

- not billed
- not counted against inference quota

This makes a REST-based token counting path operationally acceptable for `/context`.

## Official Sources

- [Gemini API: Counting tokens](https://ai.google.dev/api/tokens)
- [Gemini API: GenerateContent and usage metadata](https://ai.google.dev/api/generate-content#v1beta.CitationMetadata)
- [Gemini API docs: Text generation / multi-turn conversations](https://ai.google.dev/gemini-api/docs/text-generation)
- [Gemini API docs: Thought signatures](https://ai.google.dev/gemini-api/docs/thought-signatures)
- [Gemini API docs: Function calling](https://ai.google.dev/gemini-api/docs/function-calling)
- [Gemini API docs: Billing](https://ai.google.dev/gemini-api/docs/billing/)
- [Gemini Developer API pricing](https://ai.google.dev/pricing)
- [python-genai docs](https://googleapis.github.io/python-genai/)
- [python-genai issue discussing Gemini `count_tokens` config mismatch](https://github.com/googleapis/python-genai/issues/432)

## Recommended Interpretation

There are two different quantities we may want to show:

1. `Observed last-turn usage`
   This comes from the last real `generate_content` response metadata.
   It is authoritative for the previous request.

2. `Estimated next-turn prompt occupancy`
   This should come from counting the exact request payload that would be sent if the next model turn happened now.

These should not be conflated.

The current implementation mixes them:

- `total_tokens` comes from the previous real response
- category estimates come from separate estimation calls

That is why the residual can become negative.

## Solution Options

### Option 1: Rebuild the next request and count it via REST

Recommended.

Approach:

- rebuild the exact next `generateContent` payload
- include:
  - full message history in Gemini request shape
  - `systemInstruction`
  - `tools`
  - any required metadata such as tool call / tool response structure
- call REST `countTokens(generateContentRequest=...)`
- use that as the primary prompt occupancy number

Benefits:

- best alignment with real Gemini prompt accounting
- supports `systemInstruction` and `tools`
- no billing
- no inference quota consumption

Risks:

- requires a REST code path in the provider
- we must keep the REST payload builder aligned with the actual `generate_content` request shape

### Option 2: Keep the current SDK-based approximation, but stop using residual math

Approach:

- keep the current estimators
- do not compute `messages = total - system - skills - tools`
- show categories as approximate independent estimates
- show `unknown` when the numbers are not directly comparable

Benefits:

- minimal implementation change
- avoids negative values

Risks:

- still not measuring the real request shape
- tool estimates remain especially weak

### Option 3: Separate the UI into "Observed" and "Estimated"

Approach:

- section 1: previous request usage from `usageMetadata`
- section 2: current estimated next prompt occupancy
- never combine them into one additive breakdown unless they are computed from the same payload

Benefits:

- semantically honest
- avoids misleading arithmetic

Risks:

- more UI and explanation work

### Option 4: Use local tokenizer only as a fallback

Approach:

- when remote count is unavailable, use local tokenization for rough text-only estimates

Benefits:

- no network dependency for fallback

Risks:

- even less trustworthy for structured request accounting
- not sufficient for accurate `tools` counting

## Recommended Next Step

Recommended next implementation direction:

1. Add a Gemini REST-based `countTokens(generateContentRequest=...)` path for estimation.
2. Rebuild the exact next request payload from current session state.
3. Count the next prompt as one cohesive unit.
4. Only show additive category breakdowns if each category is counted from the same request accounting model.
5. If category subtraction still cannot be made stable, split the UI into:
   - `Last request usage`
   - `Next request estimated prompt occupancy`

## Short Conclusion

The current `/context` implementation is useful as an approximation, but it is not yet a sound context accounting model.

The core issue is not just estimator noise. The deeper issue is that the current code mixes:

- previous request metadata
- independent approximation calls

and then treats them as if they were one closed accounting system.

The direct REST experiments show a better path exists for Gemini Developer API:

- `countTokens(generateContentRequest=...)`
- with `systemInstruction`
- and `tools`
- matching real prompt usage much more closely than the current SDK fallback path.
