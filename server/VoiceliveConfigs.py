configs = {
        "type": "session.update",
        "session": {
            "instructions": """build_instructions(caller_id)""",
            "turn_detection": {
                "type": "azure_semantic_vad",

                "threshold": 0.5,
                "speech_duration_ms": 120,
                "prefix_padding_ms": 400,
                "silence_duration_ms": 400,

                "remove_filler_words": True,

                "languages": ["en", "hi"],

                "create_response": True,
                "interrupt_response": True,
                "auto_truncate": True,

                "end_of_utterance_detection": {
                    "model": "semantic_detection_v1_multilingual",
                    "threshold_level": "low",
                    "timeout_ms": 400,
                },
            },

            "input_audio_transcription": {
                "model": "azure-speech",
                "language": "hi-IN,en-IN,kn-IN,mr-IN",
                "phrase_list": [
                    "Guptaji", "गुप्ताजी", "Kavya", "Sanjana", "Archana", "काव्या",
                    "credit card", "debit", "EMI", "CVV", "OTP",
                    "statement", "due date", "minimum due", "outstanding",
                    "late payment fee", "annual fee", "finance charge",
                    "dispute", "chargeback", "auto-pay",
                    "card block", "replacement card", "credit limit",
                ],
            },

            "input_audio_noise_reduction": {
                "type": "azure_deep_noise_suppression",
            },
            "input_audio_echo_cancellation": {
                "type": "server_echo_cancellation",
            },

            "voice": {
                "name": "en-IN-Diya:DragonHDLatestNeural",
                "type": "azure-standard",
                "temperature": 0.8,
                "rate": "1.2",
            },

            "temperature": 0.1,
            "max_response_output_tokens": "750",
        },
    }
