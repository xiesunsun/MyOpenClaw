import unittest

from myopenclaw.shared.generation import (
    FinishReason,
    GenerateRequest,
    GenerateResult,
    TokenUsage,
)


class SharedGenerationTests(unittest.TestCase):
    def test_shared_module_exports_generation_protocol_types(self) -> None:
        request = GenerateRequest(system_instruction="sys", messages=[], tools=[])
        result = GenerateResult(
            text="done",
            finish_reason=FinishReason.STOP,
            provider_finish_reason="STOP",
            provider_finish_message="Model stopped normally.",
            provider_response_id="resp-1",
            provider_model_version="gemini-3-flash-preview-001",
            usage=TokenUsage(
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
            ),
        )

        self.assertEqual("sys", request.system_instruction)
        self.assertEqual("done", result.text)
        self.assertEqual(FinishReason.STOP, result.finish_reason)
        self.assertEqual(1, result.usage.input_tokens)
        self.assertEqual(3, result.usage.total_tokens)
        self.assertEqual("STOP", result.provider_finish_reason)
        self.assertEqual("resp-1", result.provider_response_id)


if __name__ == "__main__":
    unittest.main()
