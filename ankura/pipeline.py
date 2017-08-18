"""Functionality for creating import pipelines"""

import collections
import functools
import glob
import gzip
import io
import os
import pickle
import re
import string
import tarfile

import bs4

# POD types used throughout the pipeline process

Text = collections.namedtuple('Text', 'name data')
TokenLoc = collections.namedtuple('TokenLoc', 'token loc')
Document = collections.namedtuple('Document', 'text tokens metadata')
Corpus = collections.namedtuple('Corpus', 'documents vocabulary')

# Inputers are callables which generate the filenames a Pipeline should read.
# The files should be opened in binary read mode. The caller is reponsible for
# closing the file objects, although garbage collection should handle this as
# soon as the caller is finished with the file object.


def file_inputer(*filenames):
    """Generates file objects for each of the given filenames"""
    @functools.wraps(file_inputer)
    def _inputer():
        for filename in filenames:
            yield open(filename, 'rb')
    return _inputer


def glob_inputer(pattern):
    """Generates file objects for each filename matching a glob pattern"""
    return file_inputer(*glob.glob(pattern))


# Extractors are callables which generate Text from a file object. Typically
# the file objects are generated by an inputer. Extractors should take as a
# parameter a file object, and zero or more yield Text.


def whole_extractor(encoding='utf-8', errors='strict'):
    """Extracts the entire contents of a file as a single Text"""
    @functools.wraps(whole_extractor)
    def _extractor(docfile):
        yield Text(docfile.name, docfile.read().decode(encoding, errors))
    return _extractor


def skip_extractor(delim='\n\n', encoding='utf-8', errors='strict'):
    """After skipping a header, extracts the remaining contents of a file as a
    single Text
    """
    @functools.wraps(skip_extractor)
    def _extractor(docfile):
        data = docfile.read().decode(encoding, errors)
        _, data = data.split(delim, 1)
        yield Text(docfile.name, data)
    return _extractor


def line_extractor(delim=' ', encoding='utf-8', errors='strict'):
    """Treats each line of a file as a Text, regarding everything before the
    delimiter as the name of the Text, and everything after as the Text data.
    Each line is stripped of leading and trailing whitespace before processing.
    """
    @functools.wraps(line_extractor)
    def _extractor(docfile):
        for line in docfile:
            line = line.decode(encoding, errors).strip()
            name, data = line.split(delim, 1)
            yield Text(name, data)
    return _extractor


def html_extractor(encoding='utf-8', errors='strict'):
    """Extracts the text content of an HTML file as a single Text. Blank lines
    are removed from the result, and both leading and trailing whitespace are
    stripped.
    """
    newline = re.compile(r'\n\n+')
    @functools.wraps(html_extractor)
    def _extractor(docfile):
        raw = docfile.read().decode(encoding, errors)
        soup = bs4.BeautifulSoup(raw, 'html.parser')
        text = soup.get_text()
        text = newline.sub('\n', text)
        text = text.strip()
        yield Text(docfile.name, text)
    return _extractor


def gzip_extractor(base_extractor):
    """Passes the uncompressed contents of a file object to a base extractor"""
    @functools.wraps(gzip_extractor)
    def _extractor(docfile):
        return base_extractor(gzip.GzipFile(fileobj=docfile))
    return _extractor


def tar_extractor(base_extractor):
    """Passes each file in a tar archive to a base extractor and aggregates the
    results
    """
    @functools.wraps(tar_extractor)
    def _extractor(docfile):
        archive = tarfile.TarFile(fileobj=docfile, mode='r')
        for info in archive:
            if not info.isfile():
                continue
            member = io.BytesIO(archive.extractfile(info.name).read())
            member.name = info.name
            for text in base_extractor(member):
                yield text
    return _extractor


def targz_extractor(base_extractor):
    """Passse each file in a gzip compressed tar archive to a base extractor
    and aggregates the results
    """
    return gzip_extractor(tar_extractor(base_extractor))


# Tokenizers are callables which split Text data into TokenLoc. Typically the
# data is from a Text generated by an extractor. Tokenizers should take as
# input a single string, and return a list of TokenLoc.


def split_tokenizer(delims=string.whitespace):
    """Splits data on delimiting characters. The default delims are
    whitespace characters.
    """
    @functools.wraps(split_tokenizer)
    def _tokenizer(data):
        tokens = []
        begin = -1 # Set to -1 when looking for start of token
        for i, char in enumerate(data):
            if char in delims:
                if begin >= 0:
                    tokens.append(TokenLoc(data[begin: i], begin))
                    begin = -1
            elif begin == -1:
                begin = i
        if begin >= 0: # Last token might be at EOF
            tokens.append(TokenLoc(data[begin:], begin))
        return tokens
    return _tokenizer


_LOWER_DELPUNCT_TABLE = str.maketrans(string.ascii_letters,
                                      string.ascii_lowercase * 2,
                                      string.punctuation)


def translate_tokenizer(base_tokenizer, table=_LOWER_DELPUNCT_TABLE):
    """Transforms the output of another tokenizer by using string translate
    with the given mapping. Empty tokens after the translate are removed. The
    default table maps uppercase letters to lowercase, and removes punctuation
    """
    @functools.wraps(translate_tokenizer)
    def _tokenizer(data):
        tokens = base_tokenizer(data)
        tokens = [TokenLoc(t.token.translate(table), t.loc) for t in tokens]
        tokens = [t for t in tokens if t.token]
        return tokens
    return _tokenizer


def default_tokenizer():
    """Splits the data on whitespace, lowercases the tokens, and removes
    punctuation. Empty tokens are removed.
    """
    return translate_tokenizer(split_tokenizer())


def regex_tokenizer(base_tokenizer, pattern, repl):
    """Transforms the output of another tokenizer by replacing all tokens which
    match a regular expression. Note that the entire token is replaced if any
    part of it matches the regular expression, so it may be desirable to use ^
    and $ anchors to match the entire token.
    """
    combine_re = re.compile(pattern).search
    combine = lambda t: TokenLoc(repl, t.loc) if combine_re(t.token) else t
    @functools.wraps(regex_tokenizer)
    def _tokenizer(data):
        tokens = base_tokenizer(data)
        tokens = [combine(t) for t in tokens]
        return tokens
    return _tokenizer


def remove_tokenizer(base_tokenizer, pattern):
    """Transforms the output of another tokenizer by removing all tokens which
    match a regular expression. Note that the entire token is removed if any
    part of it matches the regular expression, so it may be desirable to use ^
    and $ anchors to match the entire token.
    """
    remove_re = re.compile(pattern).search
    @functools.wraps(remove_tokenizer)
    def _tokenizer(data):
        tokens = base_tokenizer(data)
        tokens = [t for t in tokens if not remove_re(t.token)]
        return tokens
    return _tokenizer


def _tokenset(tokens, strip):
    return set(t.strip() for t in tokens) if strip else set(tokens)


def combine_tokenizer(base_tokenizer, combine, repl, strip=True):
    """Transforms the output of another tokenizer by replacing all tokens which
    appear in a combine list. The optional strip parameter (default true)
    indicates whether the tokens in the combine list should have whitespace
    stripped.
    """
    combine_set = _tokenset(combine, strip)
    combine = lambda t: TokenLoc(repl, t.loc) if t.token in combine_set else t
    @functools.wraps(combine_tokenizer)
    def _tokenizer(data):
        tokens = base_tokenizer(data)
        tokens = [combine(t) for t in tokens]
        return tokens
    return _tokenizer


def stopword_tokenizer(base_tokenizer, stopwords, strip=True):
    """Transforms the output of another tokenizer by removing all tokens which
    appear in a stopword list. The optional strip parameter (default true)
    indicates whether the tokens in the stopword list should have whitespace
    stripped.
    """
    stopword_set = _tokenset(stopwords, strip)
    @functools.wraps(stopword_tokenizer)
    def _tokenizer(data):
        tokens = base_tokenizer(data)
        tokens = [t for t in tokens if t.token not in stopword_set]
        return tokens
    return _tokenizer


def frequency_tokenizer(pipeline, rare=None, common=None):
    """Transforms the output of Pipeline tokenizer to remove rare and common
    words according to the given thresholds. Rare tokens are tokens which
    appear in a smaller number of documents than the given rare threshold.
    Common tokens are those which appear in a greater number of documents than
    the given common threshold. For either threshold, the default value of None
    indicates that the threshold should be ignored.

    Note that in order to determine how many documents each token appears in,
    much of the import must be run. Consequently, the construction of this
    tokenizer may take significant time.
    """
    if rare and common:
        keep = lambda n: rare <= n <= common
    elif rare:
        keep = lambda n: rare <= n
    elif common:
        keep = lambda n: n <= common
    else:
        return pipeline.tokenizer

    counts = collections.defaultdict(int)
    for docfile in pipeline.inputer():
        for text in pipeline.extractor(docfile):
            tokens = {t.token for t in pipeline.tokenizer(text.data)}
            for token in tokens:
                counts[token] += 1

    stopwords = [token for token, count in counts.items() if not keep(count)]
    return stopword_tokenizer(pipeline.tokenizer, stopwords)


# Labelers are callables which generate metadata from a Text name. Typically
# the name is from a Text generated by an extractor. Labelers should take as
# input a single string, and return a dict containing metadata key/value pairs.


def noop_labeler():
    """Returns an empty labeling"""
    @functools.wraps(noop_labeler)
    def _labeler(_name):
        return {}
    return _labeler


def title_labeler(attr='title'):
    """Returns a labeling with the name as the value"""
    @functools.wraps(title_labeler)
    def _labeler(name):
        return {attr: name}
    return _labeler


def dir_labeler(attr='dirname'):
    """Returns a labeling with the dirname of the name as the value"""
    @functools.wraps(dir_labeler)
    def _labeler(name):
        return {attr: os.path.dirname(name)}
    return _labeler


def dict_labeler(values, attr='label'):
    """Returns a labeling using values from a dict"""
    @functools.wraps(dict_labeler)
    def _labeler(name):
        return {attr: values[name]}
    return _labeler


def string_labeler(data, delim=' ', attr='label'):
    """Builds a dict labeler from a data stream. Each line in the data should
    contain a single name/label pair, separated by the given delimiter and the
    label is string.
    """
    labels = dict(line.strip().split(delim, 1) for line in data)
    return dict_labeler(labels, attr)


def float_labeler(data, delim=' ', attr='label'):
    """Builds a dict labeler from a data stream. Each line in the data should
    contain a single name/value pair, separated by the given delimiter, and the
    value is parsable as a float.
    """
    labels = dict(line.strip().split(delim, 1) for line in data)
    labels = {name: float(value) for name, value in labels.items()}
    return dict_labeler(labels, attr)


def multistring_labeler(data, delim=' ', attr='labels'):
    """Builds a dict labeler from a data stream which maps each name to
    multiple values. Each line in the data should contain a single name/label
    pair, separated by the given delimiter. In order to associate a single name
    with multiple string labels, multiple lines are needed. Missing names
    default to an empty list.
    """
    labels = collections.defaultdict(list)
    for line in data:
        name, value = line.strip().split(delim, 1)
        labels[name].append(value)
    return dict_labeler(labels, attr)


def composite_labeler(*labelers):
    """Returns a labeling with the merged results of several labelers"""
    @functools.wraps(composite_labeler)
    def _labeler(name):
        labels = {}
        for labeler in labelers:
            labels.update(labeler(name))
        return labels
    return _labeler


# Filterers are callables which return True if a Document should be included in
# a Corpus.

def keep_filterer():
    """Always returns True reguardless of the Document"""
    @functools.wraps(keep_filterer)
    def _filterer(_doc):
        return True
    return _filterer


def length_filterer(threshold=1):
    """Returns True if the number of tokens in the document is at or above the
    given threshold. The default threshold of 1 filters out empty documents.
    """
    @functools.wraps(length_filterer)
    def _filterer(doc):
        return len(doc.tokens) >= threshold
    return _filterer


# Pipeline describes the process of importing a Corpus. It consists of an
# inputer, extractor, tokenizer, and labeler.


class VocabBuilder(object):
    """Stores a bidirectional map of token to token ids"""

    def __init__(self):
        self.tokens = []
        self.types = {}

    def __getitem__(self, token):
        if token not in self.types:
            self.types[token] = len(self.tokens)
            self.tokens.append(token)
        return self.types[token]

    def convert(self, tokens):
        """Converts a sequence of TokenLoc to use types"""
        return [TokenLoc(self[t.token], t.loc) for t in tokens]


class DocumentStream(object):
    """A file-backed document stream for large document collections"""

    def __init__(self, filename):
        self._path = filename
        self._file = open(filename, 'wb')
        self._size = 0
        self._flushed = True

    def append(self, doc):
        """Writes the document to the backing file."""
        if self._file is None:
            self._file = open(self._path, 'ab')

        pickle.dump(doc, self._file)
        self._size += 1
        self._flushed = False

    def __iter__(self):
        self._flush()

        with open(self._path, 'rb') as docs:
            for _ in range(self._size):
                yield pickle.load(docs)

    def __getstate__(self):
        self._flush()
        return (self._path, self._size)

    def __setstate__(self, state):
        self._path, self._size = state
        self._file = None
        self._flushed = False

    def _flush(self):
        if self._file is not None and not self._flushed:
            self._file.flush()
            self._flushed = True


class Pipeline(object):
    """Pipeline describes the process of importing a Corpus"""

    # pylint: disable=too-many-arguments
    def __init__(self, inputer, extractor, tokenizer, labeler, filterer):
        self.inputer = inputer
        self.extractor = extractor
        self.tokenizer = tokenizer
        self.labeler = labeler
        self.filterer = filterer

    def run(self, pickle_path=None, docs_path=None):
        """Creates a new Corpus using the Pipeline"""
        if pickle_path and os.path.exists(pickle_path):
            return pickle.load(open(pickle_path, 'rb'))

        documents = []
        documents = DocumentStream(docs_path) if docs_path else []
        vocab = VocabBuilder()
        for docfile in self.inputer():
            for text in self.extractor(docfile):
                tokens = self.tokenizer(text.data)
                types = vocab.convert(tokens)
                metadata = self.labeler(text.name)
                document = Document(text.data, types, metadata)
                if self.filterer(document):
                    documents.append(document)

        corpus = Corpus(documents, vocab.tokens)
        if pickle_path:
            pickle.dump(corpus, open(pickle_path, 'wb'))
        return corpus


# TODO Replace shelve with better performing persistant storage
# Could use shelve or sqlite3, especially if we disallow random access for docs
