import unittest

from myopenclaw.runtime import FinishReason, GenerateRequest, GenerateResult, TokenUsage


class RuntimeGenerationTests(unittest.TestCase):
    def test_runtime_exports_generation_protocol_types(self) -> None:
        request = GenerateRequest(system_instruction="sys", messages=[], tools=[])
        result = GenerateResult(
            text="done",
            finish_reason=FinishReason.STOP,
            usage=TokenUsage(input_tokens=1, output_tokens=2),
        )

        self.assertEqual("sys", request.system_instruction)
        self.assertEqual("done", result.text)
        self.assertEqual(FinishReason.STOP, result.finish_reason)
        self.assertEqual(1, result.usage.input_tokens)


if __name__ == "__main__":
    unittest.main()
