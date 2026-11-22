import unittest
from unittest.mock import patch

from tracekit.core.context import set_current_span, set_current_trace, set_trace_context
from tracekit.core.tracer import Tracer
from tracekit.integrations import langchain_auto
from tracekit.integrations.langchain import TracekitCallbackHandler


class TestLangChainAuto(unittest.TestCase):
    def setUp(self):
        langchain_auto.untrace_langchain()
        self._module_attributes = {
            name: getattr(langchain_auto, name)
            for name in (
                "_BaseLLM",
                "_BaseChain",
                "_BaseTool",
                "_LANGCHAIN_IMPORT_ERROR",
            )
        }
        self._original_inits = dict(langchain_auto._ORIGINAL_INITS)
        self._patched = langchain_auto._PATCHED
        langchain_auto._ORIGINAL_INITS.clear()
        langchain_auto._PATCHED = False
        self._clear_context()

    def tearDown(self):
        langchain_auto.untrace_langchain()
        langchain_auto._ORIGINAL_INITS.clear()
        langchain_auto._ORIGINAL_INITS.update(self._original_inits)
        langchain_auto._PATCHED = self._patched
        for name, value in self._module_attributes.items():
            setattr(langchain_auto, name, value)
        self._clear_context()

    @staticmethod
    def _clear_context():
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    @staticmethod
    def _fake_llm_class():
        class FakeBaseLLM:
            def __init__(self, callbacks=None, **kwargs):
                self.callbacks = list(callbacks or [])

        return FakeBaseLLM

    @staticmethod
    def _assert_one_handler(test_case, instance):
        handlers = [
            callback
            for callback in instance.callbacks
            if isinstance(callback, TracekitCallbackHandler)
        ]
        test_case.assertEqual(len(handlers), 1)

    def test_trace_langchain_wraps_llm(self):
        fake_cls = self._fake_llm_class()
        with (
            patch.object(langchain_auto, "_BaseLLM", fake_cls),
            patch.object(langchain_auto, "_BaseChain", None),
            patch.object(langchain_auto, "_BaseTool", None),
            patch.object(langchain_auto, "_LANGCHAIN_IMPORT_ERROR", None),
        ):
            langchain_auto.trace_langchain(tracer=Tracer())
            instance = fake_cls(callbacks=["existing"])

            self._assert_one_handler(self, instance)
            self.assertIn("existing", instance.callbacks)

    def test_trace_langchain_idempotent(self):
        fake_cls = self._fake_llm_class()
        tracer = Tracer()
        with (
            patch.object(langchain_auto, "_BaseLLM", fake_cls),
            patch.object(langchain_auto, "_BaseChain", None),
            patch.object(langchain_auto, "_BaseTool", None),
            patch.object(langchain_auto, "_LANGCHAIN_IMPORT_ERROR", None),
        ):
            langchain_auto.trace_langchain(tracer=tracer)
            original_init = langchain_auto._ORIGINAL_INITS[fake_cls]
            langchain_auto.trace_langchain(tracer=tracer)

            self.assertIs(langchain_auto._ORIGINAL_INITS[fake_cls], original_init)
            instance = fake_cls()
            self._assert_one_handler(self, instance)

    def test_untrace_langchain_restores(self):
        fake_cls = self._fake_llm_class()
        original_init = fake_cls.__init__
        with (
            patch.object(langchain_auto, "_BaseLLM", fake_cls),
            patch.object(langchain_auto, "_BaseChain", None),
            patch.object(langchain_auto, "_BaseTool", None),
            patch.object(langchain_auto, "_LANGCHAIN_IMPORT_ERROR", None),
        ):
            langchain_auto.trace_langchain(tracer=Tracer())
            self.assertIsNot(fake_cls.__init__, original_init)

            langchain_auto.untrace_langchain()
            self.assertIs(fake_cls.__init__, original_init)
            instance = fake_cls()
            self.assertFalse(
                any(
                    isinstance(callback, TracekitCallbackHandler)
                    for callback in instance.callbacks
                )
            )

    def test_trace_langchain_without_langchain_installed(self):
        with (
            patch.object(
                langchain_auto,
                "_LANGCHAIN_IMPORT_ERROR",
                ImportError("No module named 'langchain_core'"),
            ),
            patch.object(langchain_auto, "_BaseLLM", None),
            patch.object(langchain_auto, "_BaseChain", None),
            patch.object(langchain_auto, "_BaseTool", None),
        ):
            with self.assertRaises(ImportError) as raised:
                langchain_auto.trace_langchain()
            self.assertIn("pip install tracekit[langchain]", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
