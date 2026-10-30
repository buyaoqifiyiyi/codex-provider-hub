import unittest

import cc_switch_metadata as metadata


class CountingMapping(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.items_calls = 0

    def items(self):
        self.items_calls += 1
        return super().items()


def provider(api, models):
    return {"api": api, "models": models}


def model(context=None, efforts=None, modalities=None, canonical=None):
    result = {}
    if context is not None:
        result["limit"] = {"context": context}
    if efforts is not None:
        result["reasoning_options"] = [{"type": "effort", "values": efforts}]
    if modalities is not None:
        result["modalities"] = {"input": modalities}
    if canonical is not None:
        result["canonical_model_id"] = canonical
    return result


class MetadataTests(unittest.TestCase):
    def test_same_endpoint_uses_preset_first(self):
        data = {
            "openai": provider(
                "https://api.example.com/model/v1",
                {"sample-model": model(900_000, ["low", "high"])},
            )
        }
        presets = [
            {
                "base_url": "https://api.example.com/model/v1/chat/completions",
                "models": {"sample-model": {"context_window": 200_000}},
            }
        ]
        result = metadata.resolve_metadata(
            "sample-model",
            "https://api.example.com/model",
            data,
            presets,
        )
        self.assertEqual(result["context_window"], 200_000)
        self.assertEqual(result["reasoning_levels"], ["low", "high"])
        self.assertEqual(
            result["sources"],
            {
                "context_window": metadata.SOURCE_PRESET,
                "reasoning_levels": metadata.SOURCE_MODELS_DEV_PROVIDER,
                "supports_image": None,
            },
        )

    def test_provider_endpoint_ignores_version_and_request_suffix(self):
        data = {
            "proxy": provider(
                "https://API.Example.com/v1",
                {"alpha": model("128000", ["low"])},
            )
        }
        result = metadata.resolve_metadata(
            "alpha", "https://api.example.com/responses", data
        )
        self.assertEqual(result["context_window"], 128000)
        self.assertEqual(result["reasoning_levels"], ["low"])
        self.assertEqual(
            result["sources"]["context_window"],
            metadata.SOURCE_MODELS_DEV_PROVIDER,
        )

    def test_nondefault_ports_do_not_share_provider_metadata(self):
        data = {"proxy": provider("https://api.example.com:8443/v1", {"alpha": model(128000)})}
        self.assertIsNone(metadata.resolve_metadata("alpha", "https://api.example.com:9443/v1", data)["context_window"])
        self.assertEqual(metadata.resolve_metadata("alpha", "https://api.example.com:8443/v1", data)["context_window"], 128000)

    def test_model_id_normalization_finds_namespaced_variant(self):
        data = {
            "proxy": provider(
                "https://api.example.com/v1",
                {
                    "example-model-x@latest[1m]": model(
                        400_000, ["low", "high"], ["text", "image"]
                    )
                },
            )
        }
        result = metadata.resolve_metadata(
            "OPENAI/EXAMPLE-MODEL-X@latest[1m]:high",
            "https://api.example.com/v1",
            data,
        )
        self.assertEqual(result["context_window"], 400_000)
        self.assertEqual(result["reasoning_levels"], ["low", "high"])
        self.assertIs(result["supports_image"], True)
        self.assertEqual(
            result["sources"]["context_window"],
            metadata.SOURCE_MODELS_DEV_PROVIDER,
        )

    def test_vendor_reference_keeps_reported_context_without_model_specific_caps(self):
        data = {
            "proxy": provider(
                "https://proxy.invalid/v1",
                {
                    "example-model-x": model(
                        900_000, canonical="vendor/example-model-x"
                    )
                },
            ),
            "vendor": provider(
                "https://vendor.invalid/v1",
                {
                    "example-model-x": model(
                        1_050_000, ["low", "high"], ["text", "image"]
                    )
                },
            ),
        }
        vendor_only = metadata.resolve_metadata(
            "example-model-x", "https://unknown.invalid/v1", data
        )
        self.assertEqual(vendor_only["context_window"], 1_050_000)
        self.assertEqual(
            vendor_only["sources"]["context_window"],
            metadata.SOURCE_MODELS_DEV_VENDOR,
        )

        provider_value = metadata.resolve_metadata(
            "example-model-x", "https://proxy.invalid/v1", data
        )
        self.assertEqual(provider_value["context_window"], 900_000)
        self.assertEqual(
            provider_value["sources"]["context_window"],
            metadata.SOURCE_MODELS_DEV_PROVIDER,
        )

        preset_value = metadata.resolve_metadata(
            "example-model-x",
            "https://vendor.invalid/v1",
            data,
            [
                {
                    "base_url": "https://vendor.invalid/v1",
                    "models": {"example-model-x": {"context_window": 1_200_000}},
                }
            ],
        )
        self.assertEqual(preset_value["context_window"], 1_200_000)
        self.assertEqual(
            preset_value["sources"]["context_window"],
            metadata.SOURCE_PRESET,
        )

    def test_vendor_fallback_requires_one_vendor(self):
        data = {
            "one": provider(
                "https://one.invalid/v1",
                {"same": model(100_000, canonical="uno/same")},
            ),
            "uno": provider(
                "https://uno.invalid/v1", {"same": model(200_000)}
            ),
            "two": provider(
                "https://two.invalid/v1",
                {"same": model(300_000, canonical="dos/same")},
            ),
            "dos": provider(
                "https://dos.invalid/v1", {"same": model(400_000)}
            ),
        }
        result = metadata.resolve_metadata(
            "same", "https://unrecognized.invalid/v1", data
        )
        self.assertEqual(
            result,
            {
                "context_window": None,
                "reasoning_levels": None,
                "supports_image": None,
                "sources": {
                    "context_window": None,
                    "reasoning_levels": None,
                    "supports_image": None,
                },
            },
        )

    def test_shared_endpoint_with_multiple_model_matches_is_unknown(self):
        data = {
            "one": provider(
                "https://shared.invalid/v1", {"same": model(100_000)}
            ),
            "two": provider(
                "https://shared.invalid/v1", {"same": model(200_000)}
            ),
            "three": provider(
                "https://shared.invalid/v1", {"other": model(300_000)}
            ),
        }
        ambiguous = metadata.resolve_metadata(
            "same", "https://shared.invalid/v1", data
        )
        self.assertIsNone(ambiguous["context_window"])
        self.assertIsNone(ambiguous["sources"]["context_window"])
        unique = metadata.resolve_metadata(
            "other", "https://shared.invalid/v1", data
        )
        self.assertEqual(unique["context_window"], 300_000)
        self.assertEqual(
            unique["sources"]["context_window"],
            metadata.SOURCE_MODELS_DEV_PROVIDER,
        )

    def test_model_metadata_keeps_recognized_efforts(self):
        data = {
            "vendor": provider(
                "https://vendor.invalid/v1",
                {
                    "example-model-a": model(
                        128_000, ["low", "medium", "high", "max", "ultra"]
                    ),
                    "example-model-b": model(
                        256_000, ["turbo", "low", "max", "high"]
                    ),
                },
            )
        }
        result = metadata.resolve_metadata(
            "Vendor/Example-Model-A:max",
            "https://vendor.invalid/v1",
            data,
        )
        self.assertEqual(result["reasoning_levels"], ["low", "medium", "high", "max", "ultra"])

        invalid = metadata.resolve_metadata(
            "example-model-b", "https://vendor.invalid/v1", data
        )
        self.assertEqual(invalid["reasoning_levels"], ["low", "max", "high"])

    def test_rejects_non_allowed_reasoning_and_reports_image_unknown(self):
        data = {
            "proxy": provider(
                "https://api.example.com/v1",
                {
                    "unsupported": model(64_000, ["turbo", "extreme"]),
                    "text-only": model(64_000, ["high"], ["text"]),
                    "bad-shapes": {
                        "limit": ["not", "a", "mapping"],
                        "reasoning_options": {"type": "effort", "values": ["high"]},
                        "modalities": {"input": "image"},
                    },
                    "huge": {"limit": {"context": 10**1000}},
                },
            )
        }
        unsupported = metadata.resolve_metadata(
            "unsupported", "https://api.example.com/v1", data
        )
        self.assertIsNone(unsupported["reasoning_levels"])
        text_only = metadata.resolve_metadata(
            "text-only", "https://api.example.com/v1", data
        )
        self.assertIs(text_only["supports_image"], False)
        malformed = metadata.resolve_metadata(
            "bad-shapes", "https://api.example.com/v1", data
        )
        self.assertIsNone(malformed["context_window"])
        self.assertIsNone(malformed["reasoning_levels"])
        self.assertIsNone(malformed["supports_image"])
        huge = metadata.resolve_metadata(
            "huge", "https://api.example.com/v1", data
        )
        self.assertIsNone(huge["context_window"])

    def test_field_sources_can_come_from_all_three_tiers(self):
        data = {
            "proxy": provider(
                "https://proxy.invalid/v1",
                {
                    "example-model-x": model(
                        900_000,
                        ["medium", "high"],
                        canonical="vendor/example-model-x",
                    )
                },
            ),
            "vendor": provider(
                "https://vendor.invalid/v1",
                {
                    "example-model-x": model(
                        1_050_000, ["high"], ["text", "image"]
                    )
                },
            ),
        }
        result = metadata.resolve_metadata(
            "example-model-x",
            "https://proxy.invalid/v1",
            data,
            [
                {
                    "base_url": "https://proxy.invalid/responses",
                    "models": {"example-model-x": {"context_window": 200_000}},
                }
            ],
        )
        self.assertEqual(
            result,
            {
                "context_window": 200_000,
                "reasoning_levels": ["medium", "high"],
                "supports_image": True,
                "sources": {
                    "context_window": metadata.SOURCE_PRESET,
                    "reasoning_levels": metadata.SOURCE_MODELS_DEV_PROVIDER,
                    "supports_image": metadata.SOURCE_MODELS_DEV_VENDOR,
                },
            },
        )

    def test_malformed_documents_and_urls_are_unknown_not_exceptions(self):
        malformed_data = {
            "none": None,
            "wrong-models": {"api": "https://bad.invalid", "models": [1, 2]},
            42: provider("https://ignored.invalid", {"wrong-key": model()}),
            "wrong-model": provider(
                "https://wrong.invalid", {"ok": None, "bad": model()}
            ),
        }
        index = metadata.build_metadata_index(malformed_data)
        self.assertIsInstance(index, metadata.MetadataIndex)
        cases = [
            (None, "https://api.example.com/v1", {}, None, None),
            ("", "https://api.example.com/v1", {}, None, None),
            ("ok", "https://[bad", malformed_data, None, None),
            ("ok", "https://wrong.invalid", malformed_data, ["bad"], None),
            ("ok", "https://wrong.invalid", malformed_data, None, object()),
        ]
        for model_id, base_url, data, presets, explicit_index in cases:
            with self.subTest(
                model_id=model_id, base_url=base_url, presets=presets
            ):
                result = metadata.resolve_metadata(
                    model_id,
                    base_url,
                    data,
                    presets,
                    index=explicit_index,
                )
                self.assertEqual(
                    result,
                    {
                        "context_window": None,
                        "reasoning_levels": None,
                        "supports_image": None,
                        "sources": {
                            "context_window": None,
                            "reasoning_levels": None,
                            "supports_image": None,
                        },
                    },
                )

    def test_many_models_reuse_one_index_without_rescanning_document(self):
        models = {
            f"model-{number}": model(number + 1, ["high"], ["text"])
            for number in range(5_000)
        }
        data = CountingMapping(
            {"proxy": provider("https://api.example.com/v1", models)}
        )
        index = metadata.build_metadata_index(data)
        self.assertEqual(data.items_calls, 1)
        for number in (0, 1, 2_500, 4_999):
            result = metadata.resolve_metadata(
                f"model-{number}",
                "https://api.example.com/v1",
                index=index,
            )
            self.assertEqual(result["context_window"], number + 1)
            self.assertEqual(
                result["sources"]["context_window"],
                metadata.SOURCE_MODELS_DEV_PROVIDER,
            )
        self.assertEqual(data.items_calls, 1)


if __name__ == "__main__":
    unittest.main()
