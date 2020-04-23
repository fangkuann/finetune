from finetune.target_models.sequence_labeling import SequenceLabeler, SequencePipeline
from finetune.encoding.input_encoder import EncodedOutput

def get_context(document, dpi_norm):
    context = []
    for page in document:
        for token in page["tokens"]:
            pos = token["position"]
            offset = token["doc_offset"]
            if dpi_norm:
                dpi = page["pages"][0]["dpi"]
                x_norm = 300 / dpi["dpix"]
                y_norm = 300 / dpi["dpiy"]
            else:
                x_norm = 1.
                y_norm = 1.

            context.append(
                {
                    'top': pos["top"] * y_norm,
                    'bottom': pos["bottom"] * y_norm,
                    'left': pos["left"] * x_norm,
                    'right': pos["right"] * x_norm,
                    'start': offset["start"],
                    'end': offset["end"],
                    'text': token["text"],
                }
	    )
    return context

def _single_convert_to_finetune(*, document, dpi_norm=True):
    context = get_context(document, dpi_norm)
    texts = []
    offsets = []
    last_end = -1
    for page in document:
        page_obj = page["pages"][0]
        texts.append(page_obj["text"] + "\n")
        offset = page_obj["doc_offset"]
        assert offset["start"] == last_end + 1, "If ever this ceases to hold then we have a problem"
        last_end = offset["end"]
    return texts, context


class DocumentPipeline(SequencePipeline):
    def __init__(self, config, multi_label):
        super().__init__(config, multi_label)

    def text_to_tokens_mask(self, raw_text=None, **kwargs):
        return super().text_to_tokens_mask(**kwargs)

    def zip_list_to_dict(self, X, Y=None, context=None):
        assert context is None
        if Y is not None:
            Y = list(Y)
            if len(X) != len(Y):
                raise FinetuneError("the length of your labels does not match the length of your text")

        out = []
        for i, x in enumerate(X):
            text, context = _single_convert_to_finetune(
                document=x,
            )
            joined_text = "".join(text)
            sample = {
                "X": text,
                "raw_text": joined_text,
                # This is done to allow chunk long sequences to rejoin labels for us.
            }
            if self.config.default_context:
                for cii in context:
                    assert cii["text"] == joined_text[cii["start"]: cii["end"]]
                sample["context"] = context
                
            if Y is not None:
                for yii in Y[i]:
                    assert yii["text"] == joined_text[yii["start"]: yii["end"]]
                sample["Y"] = Y[i]
            out.append(sample)
        return out
    
    def _text_to_ids(self, X, pad_token=None):
        offset = 0
        for X_page in X:
            for chunk in super()._text_to_ids(X_page, pad_token):
                assert len(chunk.token_starts) == len(chunk.token_ends)
                chunk_dict = chunk._asdict()
                chunk_dict["token_starts"] = [start + offset for start in chunk_dict["token_starts"]]
                chunk_dict["token_ends"] = [start + offset for start in chunk_dict["token_ends"]]
                yield EncodedOutput(**chunk_dict)
            offset += len(X_page)

class DocumentLabeler(SequenceLabeler):
    """
    A wrapper to use SequenceLabeler ontop of indico's PDFExtraction APi 
    in ondocument mode with labels at a document charachter level.
    """
    def _get_input_pipeline(self):
        return DocumentPipeline(
            config=self.config, multi_label=self.config.multi_label_sequences
        )