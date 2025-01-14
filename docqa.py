import math
from typing import List, Tuple
from PIL import Image

from tqdm import tqdm
import numpy as np
import pypdfium2 as pdfium
import os
import pytesseract
import openai
# os.environ['OPENAI_API_KEY'] = "" #   Your API Key
# openai.api_key = os.environ['OPENAI_API_KEY']
class BaseEmbedding:
    def __init__(self, model: str="text-embedding-ada-002"):
        self.model = model
        openai.api_key = os.environ['OPENAI_API_KEY']

    def get_embedding(self, text):
        text = text.replace("\n", " ")
        return openai.Embedding.create(input=[text], model=self.model)['data'][0]['embedding']

class DocInput:
    """
    Takes PDF/Image input; extracts text info from it, does chunking and sends forward
    """
    def __init__(self,file: str, chunk_size: int=128, chunk_overlap: int=10):
        extension = file.split(".")[-1]
        self.text = ""
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        if extension in ["jpg", "jpeg", "png", "tiff"]:
            self.text = pytesseract.image_to_string(Image.open(file))
        elif extension == "pdf":
            pdf = pdfium.PdfDocument(file)
            for i in tqdm(range(len(pdf))):
                page = pdf[i]
                pil_image = page.render(scale=1).to_pil()
                self.text += pytesseract.image_to_string(pil_image)
        elif extension in ["doc", "docx"]:
            import textract
            self.text = textract.process(file).decode()
        elif extension == "txt":
            self.text = open(file, "r").read()
        elif extension == "rtf":
            from striprtf.striprtf import rtf_to_text
            self.text = rtf_to_text(open(file, "r").read())
        elif extension == "odt":
            from odf import text, teletype
            from odf.opendocument import load
            textdoc = load(file)
            allparas = textdoc.getElementsByType(text.P)
            self.text = " ".join([teletype.extractText(allparas[i]) for i in range(len(allparas))])
        else:
            raise Exception("Doesnot support this format", extension)
        self.text = self.text.replace('\n', ' ')

    # def get_ocr(self,ocr_engine: str="tesseract") -> str:
    #     return ocr_output

    def get_chunks(self):
        self.doc_chunks = []
        words = self.text.split(" ")
        words = [word for word in words if word not in ['.','?','!',',',':',';']]
        self.doc_chunks = [
            " ".join(
                        words[
                            max(0, i*self.chunk_size-self.chunk_overlap)
                            :min(len(words),(i+1)*self.chunk_size)
                        ]
                    )
            for i in range(math.ceil(len(words)/self.chunk_size))
        ]

    def preprocess_chunks(self):
        # Preprocessing; nothing added for now; operate on self.doc_chunks
        pass

    def get_doc_input(self) -> List[str]:
        self.get_chunks()
        self.preprocess_chunks()

        return self.doc_chunks

class IndexDocument:
    """
    Indexes the chunks, creates dictionary with text, vectors and other metadata. 
    Returns the List of dictionary and vector embedding matrix
    """

    def __init__(
            self, 
            chunks: List[str], 
            embedding_object: BaseEmbedding):
        self.chunks = chunks
        self.embedding_object = embedding_object

    def indexed_document(self) -> Tuple[List[str], np.ndarray]:
        indexes, index_matrix = [], []
        
        for chunk in self.chunks:
            chunk_vector = self.embedding_object.get_embedding(chunk)
            indexes.append(chunk)
            index_matrix.append(chunk_vector)
        index_matrix = np.array(index_matrix)

        return indexes, index_matrix
    
class TopChunks:
    """
    Bassed on the query chunk and the indexed document. Return the chunks with the best similarity to the question
    """
    def __init__(self, 
                 indexes: List[dict], 
                 index_matrix: np.ndarray, 
                 embedding_obj: BaseEmbedding, 
                 metric: str="cosine"):
        self.indexes = indexes
        self.indexes_matrix = index_matrix
        self.embedding_obj = embedding_obj
        self.metric = metric

        if self.metric != "cosine":
            raise Exception("Metric is not supported: ", self.metric)
    
    def cosine_similarity(self, query_vector: np.ndarray):
        "Cosine similarity"
        cosines = np.matmul(self.indexes_matrix, query_vector)/(
            np.linalg.norm(self.indexes_matrix, axis=1)*np.linalg.norm(query_vector))
        return cosines

    def top_k(self, query: str, k: int=5) -> List[str]:
        "Top k chunks based on similarity metric"
        query_vector = np.asarray(self.embedding_obj.get_embedding(query))
        cosine_vector = self.cosine_similarity(query_vector)
        top_indices = np.argsort(cosine_vector)[::-1][:k]
        top_chunks = [self.indexes[i] for i in list(top_indices)]
        return top_chunks

    def top_threshold(self, query: str, threshold: float=0.9) -> List[str]:
        "Top chunks passing the similarity threshold"
        raise Exception("Top Threshold Not Implemented")

    def top_k_threshold(self, query: str, k: int=5, threshold: float=0.9) -> List[str]:
        "Top k chunks passing the threshold"
        raise Exception("Top K Threshold Not Implemented")

class DocQA:
    """
    Creates a DocQA instance with all document indexed and processed. Receives the query and answers it
    """
    def __init__(self, file: str):
        self.file = file
        self.embedding_obj = BaseEmbedding()
        openai.api_key = os.environ['OPENAI_API_KEY']
        self.post_init()

    def post_init(self):
        "Document indexing"
        doc_input = DocInput(self.file)
        self.chunks = doc_input.get_doc_input()

        index_document = IndexDocument(self.chunks, self.embedding_obj)
        self.index, self.index_matrix = index_document.indexed_document()

        self.top_chunks = TopChunks(
            indexes=self.index, 
            index_matrix=self.index_matrix, 
            embedding_obj=self.embedding_obj)

    def answer_query(self, query: str) -> str:
        answer_chunks = self.top_chunks.top_k(query)

        pointer_prompt = "\n ".join([str(i+1)+". "+chunk for i, chunk in enumerate(answer_chunks)])
        # Base prompt based on vector retrieval
        base_prompt = "These are the relevant chunks from the document:\n" + pointer_prompt
        # Adding the initial query to the base prompt
        init_prompt = base_prompt + "\nAnswer this query based on the above information:\n" + query
        # Adding preventive prompt
        final_prompt = init_prompt + "\nGive your answer based only on the above information, do not use any other information"

        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                    {"role": "system", "content": "You are a question answering bot"},
                    {"role": "user", "content": final_prompt},
                ],
            temperature=0.0,
        )

        return response
    
if __name__ == "__main__":
    # test for images
    doc_qa = DocQA("tests/test.jpg")
    response = doc_qa.answer_query("What are the things talked about luxury in this document?")
    print(response)

    # test for pdf
    doc_qa = DocQA("tests/test.pdf")
    response = doc_qa.answer_query("What are the things talked about luxury in this document?")
    print(response)

    # test for doc
    doc_qa = DocQA("tests/test.docx")
    response = doc_qa.answer_query("What are the things talked about luxury in this document?")
    print(response)

    # test for txt
    doc_qa = DocQA("tests/test.txt")
    response = doc_qa.answer_query("What are the things talked about luxury in this document?")
    print(response)

    # test for rtf
    doc_qa = DocQA("tests/test.rtf")
    response = doc_qa.answer_query("What are the things talked about luxury in this document?")
    print(response)

    # test for odt
    doc_qa = DocQA("tests/test.odt")
    response = doc_qa.answer_query("What are the things talked about luxury in this document?")
    print(response)