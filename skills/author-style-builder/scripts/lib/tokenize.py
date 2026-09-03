"""日本語テキストの文分割・形態素解析の抽象化。

sudachipy(mode C)が利用可能なら sudachi モード、無ければ fallback モード。
fallback では tokenize() は空リストを返し、POS 依存特徴は上位層で null になる。

Token = {"surface", "pos"(大分類), "pos_detail"(中分類), "base"(語彙素/辞書形),
         "pos_full"(6 要素の品詞タプル), "cform"(活用形。非活用は "*"),
         "start", "end"(tokenize に渡した文字列内のオフセット)}
"""

from __future__ import annotations

_SENT_ENDERS = "。！？!?"
_CLOSERS = "」』）)】"


def split_sentences(text: str) -> list[dict]:
    """「。！？!?」+ 改行で文分割。オフセット付き。

    返り値: [{"text", "char_start", "char_end"}, ...]
    char_start/char_end は入力 text 内のオフセット(end は排他的)。
    """
    sentences: list[dict] = []
    n = len(text)
    i = 0
    start: int | None = None

    def _emit(s: int, e: int) -> None:
        # 末尾の空白を落としてオフセットを詰める
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            sentences.append({"text": text[s:e], "char_start": s, "char_end": e})

    while i < n:
        ch = text[i]
        if start is None:
            if not ch.isspace():
                start = i
            i += 1
            continue
        if ch == "\n":
            _emit(start, i)
            start = None
            i += 1
            continue
        if ch in _SENT_ENDERS:
            j = i + 1
            while j < n and text[j] in _SENT_ENDERS:
                j += 1
            while j < n and text[j] in _CLOSERS:
                j += 1
            _emit(start, j)
            start = None
            i = j
            continue
        i += 1
    if start is not None:
        _emit(start, n)
    return sentences


class FallbackAnalyzer:
    """sudachipy 不在時の解析器。tokenize は空リストを返す。"""

    mode = "fallback"

    def meta(self) -> dict:
        return {"mode": "fallback", "version": None, "dict": None, "split_mode": None}

    def tokenize(self, text: str) -> list[dict]:
        return []


class SudachiAnalyzer:
    """SudachiPy(分割モード C)による解析器。"""

    mode = "sudachi"

    def __init__(self) -> None:
        import sudachipy
        from sudachipy import dictionary, tokenizer

        self._tokenizer = dictionary.Dictionary().create()
        self._split_mode = tokenizer.Tokenizer.SplitMode.C
        self._version = getattr(sudachipy, "__version__", "unknown")
        try:
            from importlib.metadata import version as _pkg_version

            self._dict = "sudachidict_core==" + _pkg_version("sudachidict_core")
        except Exception:
            self._dict = "sudachidict_core"

    def meta(self) -> dict:
        return {
            "mode": "sudachi",
            "version": self._version,
            "dict": self._dict,
            "split_mode": "C",
        }

    def tokenize(self, text: str) -> list[dict]:
        tokens: list[dict] = []
        for m in self._tokenizer.tokenize(text, self._split_mode):
            pos = m.part_of_speech()
            tokens.append(
                {
                    "surface": m.surface(),
                    "pos": pos[0],
                    "pos_detail": pos[1] if len(pos) > 1 else "*",
                    "base": m.dictionary_form(),
                    "pos_full": list(pos),
                    "cform": pos[5] if len(pos) > 5 else "*",
                    "start": m.begin(),
                    "end": m.end(),
                }
            )
        return tokens


ANALYZER_ENV = "DOC_STYLE_ANALYZER"  # "fallback" で sudachipy があっても fallback を強制(テスト・互換検証用)


def get_analyzer():
    """sudachipy を試み、失敗したら fallback を返す。

    環境変数 DOC_STYLE_ANALYZER=fallback で fallback を強制できる(較正と実行の
    解析器不一致を再現するテスト用)。
    """
    import os

    if os.environ.get(ANALYZER_ENV, "").lower() == "fallback":
        return FallbackAnalyzer()
    try:
        return SudachiAnalyzer()
    except Exception:
        return FallbackAnalyzer()
