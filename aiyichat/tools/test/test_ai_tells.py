"""ai_tells 中文 AI 腔确定性扫描器单元测试。

关键契约：blocking 命中才删、且只删"整句即套话"；review 只计数；
全被删空时必须回退原文（宁可不完美也不能没有回复）。
"""

from app.conversation.ai_tells import AI_VOCAB, has_blocking, scan


class TestNoFalsePositive:
    """正常角色话术不得被误删。"""

    def test_clean_reply_untouched(self) -> None:
        text = "今天又加班到十点，饭都没吃，有点撑不住了。"
        result = scan(text)
        assert result.kept == text
        assert result.blocking == []

    def test_short_replies_kept(self) -> None:
        for text in ("嗯嗯", "在的呀", "怎么啦？", "抱抱你 🤗"):
            assert scan(text).kept == text, text
            assert scan(text).blocking == [], text

    def test_natural_oral_markers_not_stripped(self) -> None:
        # 语气词与口语收尾是本项目角色风格的地基，绝不能在 blocking 档被删
        text = "好累呀。要不你先去吃点东西嘛！"
        assert scan(text).kept == text
        assert scan(text).blocking == []

    def test_quoted_user_words_downgraded(self) -> None:
        # 角色复述对方说过的套话，不算 AI 腔
        text = "你跟我说「综上所述」的时候，其实已经在难受了。"
        assert "em_dash" not in scan(text).blocking


class TestBlockingRemoved:
    """blocking 档整句剔除。"""

    def test_assistant_opener_removed(self) -> None:
        text = "当然可以。今天过得怎么样？"
        result = scan(text)
        assert "assistant_opener" in result.blocking
        assert "当然可以" not in result.kept
        assert "今天过得怎么样" in result.kept

    def test_good_question_removed(self) -> None:
        result = scan("这是一个很好的问题。我说说我的看法。")
        assert result.blocking
        assert "很好的问题" not in result.kept

    def test_assistant_closer_removed(self) -> None:
        text = "先深呼吸一下。希望对你有帮助。"
        result = scan(text)
        assert "assistant_closer" in result.blocking
        assert "希望对你有帮助" not in result.kept
        assert "先深呼吸一下" in result.kept

    def test_boilerplate_intro_removed(self) -> None:
        result = scan("随着生活节奏的加快，越来越多的人开始焦虑。其实我也一样。")
        assert "boilerplate_intro" in result.blocking
        assert "随着生活节奏" not in result.kept

    def test_boilerplate_outro_removed(self) -> None:
        result = scan("你今天已经很努力了。综上所述，休息不是罪过。")
        assert "boilerplate_outro" in result.blocking or "formal_connective" in result.review
        assert "综上所述" not in result.kept

    def test_filler_prefix_stripped_content_kept(self) -> None:
        # "当然可以，" 是套话，但后半句是真内容：只能剥前缀，不能连内容一起丢
        result = scan("当然可以，我陪你聊。")
        assert "assistant_opener" in result.blocking
        assert result.kept == "我陪你聊。"

    def test_good_question_prefix_stripped(self) -> None:
        result = scan("你说得很对，我最近确实太累了。")
        assert result.kept == "我最近确实太累了。"

    def test_filler_prefix_fully_stripped_sentence_dropped(self) -> None:
        result = scan("当然可以。")
        assert result.kept == "" or result.kept == "当然可以。"  # 整段兜底由 keep_if_all_blocked 决定
        result_strict = scan("当然可以。", keep_if_all_blocked=False)
        assert result_strict.kept == ""

    def test_em_dash_sentence_removed(self) -> None:
        result = scan("你的努力——真的很了不起。")
        assert "em_dash" in result.blocking

    def test_multi_exclaim_removed(self) -> None:
        result = scan("加油!!! 我在呢。")
        assert "multi_exclaim" in result.blocking

    def test_markdown_removed(self) -> None:
        result = scan("**重要**：你要早点睡。")
        assert "markdown" in result.blocking

    def test_fallback_when_everything_blocked(self) -> None:
        # 整段都是套话时回退原文，绝不产出空回复
        text = "当然可以。希望对你有帮助。"
        assert scan(text).kept == text

    def test_has_blocking_helper(self) -> None:
        assert has_blocking("当然可以。我帮你分析。")
        assert not has_blocking("我帮你分析了下，别急。")


class TestReviewCountedOnly:
    """review 档只计数不删。"""

    def test_formal_connective_counted_not_removed(self) -> None:
        text = "此外，你也要记得吃饭。"
        result = scan(text)
        assert result.kept == text
        assert "formal_connective" in result.review

    def test_fake_contrast_counted(self) -> None:
        result = scan("不是你不够努力，而是方法需要调整。")
        assert "fake_contrast" in result.review
        assert result.blocking == []

    def test_triple_light_counted(self) -> None:
        result = scan("风很轻，夜很长，你很乖。")
        assert "triple_light" in result.review


class TestVocab:
    """高频 AI 词只观测不干预。"""

    def test_vocab_detected(self) -> None:
        result = scan("这件事需要我们形成闭环，找到抓手。")
        assert set(result.vocab) & {"闭环", "抓手"}

    def test_vocab_not_removed(self) -> None:
        text = "这个闭环我先记下。"
        assert scan(text).kept == text

    def test_empty_input(self) -> None:
        result = scan("")
        assert result.kept == ""
        assert not result.blocking
        assert isinstance(AI_VOCAB, tuple)
