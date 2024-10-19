from __future__ import annotations

import json
from typing import Any, override

import click
from dotenv import load_dotenv
from google.auth.credentials import TokenState
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from langchain.chains import create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_community.vectorstores import FAISS
from langchain_core.prompts.chat import ChatPromptTemplate
from langchain_googledrive.retrievers import GoogleDriveRetriever
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pangea import PangeaConfig
from pangea.services import Vault
from pangea.services.vault.models.common import ItemType
from pydantic import SecretStr

load_dotenv(override=True)


PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "human",
            """You are an assistant for question-answering tasks. Use the following pieces of retrieved context to answer the question. If you don't know the answer, just say that you don't know and that the user may not be authorized to know the answer. Use three sentences maximum and keep the answer concise.
Question: {input}
Context: {context}
Answer:""",
        ),
    ]
)


class SecretStrParamType(click.ParamType):
    name = "secret"

    @override
    def convert(self, value: Any, param: click.Parameter | None = None, ctx: click.Context | None = None) -> SecretStr:
        if isinstance(value, SecretStr):
            return value

        return SecretStr(value)


SECRET_STR = SecretStrParamType()


@click.command()
@click.option(
    "--google-drive-folder-id",
    type=str,
    required=True,
    help="The ID of the Google Drive folder to fetch documents from.",
)
@click.option(
    "--vault-item-id",
    type=str,
    required=True,
    help="The item ID of the Google Drive credentials in Pangea Vault.",
)
@click.option(
    "--vault-token",
    envvar="PANGEA_VAULT_TOKEN",
    type=SECRET_STR,
    required=True,
    help="Pangea Vault API token. May also be set via the `PANGEA_VAULT_TOKEN` environment variable.",
)
@click.option(
    "--pangea-domain",
    envvar="PANGEA_DOMAIN",
    default="aws.us.pangea.cloud",
    show_default=True,
    required=True,
    help="Pangea API domain. May also be set via the `PANGEA_DOMAIN` environment variable.",
)
@click.option("--model", default="gpt-4o-mini", show_default=True, required=True, help="OpenAI model.")
@click.option(
    "--openai-api-key",
    envvar="OPENAI_API_KEY",
    type=SECRET_STR,
    required=True,
    help="OpenAI API key. May also be set via the `OPENAI_API_KEY` environment variable.",
)
@click.argument("prompt")
def main(
    *,
    prompt: str,
    google_drive_folder_id: str,
    vault_item_id: str,
    vault_token: SecretStr,
    pangea_domain: str,
    model: str,
    openai_api_key: SecretStr,
) -> None:
    # Fetch service account credentials from Pangea Vault.
    vault = Vault(token=vault_token.get_secret_value(), config=PangeaConfig(domain=pangea_domain))
    vault_result = vault.get_bulk({"id": vault_item_id}, size=1).result
    assert vault_result
    assert vault_result.items[0].type == ItemType.SECRET
    raw_gdrive_cred = vault_result.items[0].item_versions[-1].secret
    assert raw_gdrive_cred

    # Authenticate with Google Drive.
    parsed_gdrive_cred = service_account.Credentials.from_service_account_info(
        json.loads(raw_gdrive_cred),
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    parsed_gdrive_cred.refresh(Request())
    assert parsed_gdrive_cred.token_state == TokenState.FRESH

    # Fetch documents.
    google_drive_retriever = GoogleDriveRetriever(
        credentials=parsed_gdrive_cred,
        folder_id=google_drive_folder_id,
        includeItemsFromAllDrives=True,
        mode="documents-markdown",
        recursive=True,
        template="gdrive-all-in-folder",
    )
    docs = google_drive_retriever.invoke("")

    # Add them all to a vector store.
    embeddings_model = OpenAIEmbeddings(api_key=openai_api_key)
    vector_store = FAISS.from_documents(docs, embeddings_model)
    vector_store_retriever = vector_store.as_retriever()

    # Create the chain.
    llm = ChatOpenAI(model=model, api_key=openai_api_key)
    qa_chain = create_stuff_documents_chain(llm, PROMPT)
    rag_chain = create_retrieval_chain(vector_store_retriever, qa_chain)

    click.echo(rag_chain.invoke({"input": prompt})["answer"])


if __name__ == "__main__":
    main()
