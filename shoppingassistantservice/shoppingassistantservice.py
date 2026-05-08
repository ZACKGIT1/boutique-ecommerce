#!/usr/bin/python
#
# Copyright 2024 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import json
import boto3
import time
from urllib.parse import unquote
from flask import Flask, request

# LangChain AWS imports
from langchain_aws import ChatBedrock, BedrockEmbeddings
from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine, text

# AWS Secrets Manager
from botocore.exceptions import ClientError

# ===== AWS CONFIGURATION (via environment variables) =====
AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-sonnet-20240229-v1:0")
EMBEDDING_MODEL_ID = os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")

# RDS PostgreSQL Configuration
RDS_HOST = os.environ["RDS_HOST"]
RDS_PORT = os.environ.get("RDS_PORT", "5432")
RDS_DATABASE = os.environ["RDS_DATABASE"]
RDS_USER = os.environ.get("RDS_USER", "postgres")
RDS_SECRET_NAME = os.environ["RDS_SECRET_NAME"]
VECTOR_TABLE_NAME = os.environ.get("VECTOR_TABLE_NAME", "products")
PRODUCTS_JSON_PATH = os.environ.get(
    "PRODUCTS_JSON_PATH",
    os.path.join(os.path.dirname(__file__), "products.json"),
)
VECTOR_DIMENSION = int(os.environ.get("VECTOR_DIMENSION", "1024"))
SEED_EMBEDDINGS = os.environ.get("SEED_EMBEDDINGS", "false").lower() == "true"


# ===== AWS Secrets Manager Helper =====
def get_db_password(secret_name: str, region_name: str) -> str:
    """Fetch database password from AWS Secrets Manager"""
    session = boto3.session.Session()
    client = session.client(service_name="secretsmanager", region_name=region_name)
    
    try:
        response = client.get_secret_value(SecretId=secret_name)
        secret = json.loads(response["SecretString"])
        return secret["password"]
    except ClientError as e:
        print(f"❌ Error retrieving secret: {e}")
        raise


# ===== Database Connection Setup =====
DB_PASSWORD = get_db_password(RDS_SECRET_NAME, AWS_REGION)
DATABASE_URL = f"postgresql+psycopg://{RDS_USER}:{DB_PASSWORD}@{RDS_HOST}:{RDS_PORT}/{RDS_DATABASE}"

# Create SQLAlchemy engine with connection pooling
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)
embeddings = BedrockEmbeddings(model_id=EMBEDDING_MODEL_ID, region_name=AWS_REGION)


def price_to_decimal(price_usd: dict) -> float:
    units = price_usd.get("units", 0)
    nanos = price_usd.get("nanos", 0)
    return float(units) + (float(nanos) / 1_000_000_000)


def embed_with_retry(text_to_embed: str, attempts: int = 3) -> list[float] | None:
    for attempt in range(1, attempts + 1):
        try:
            return embeddings.embed_query(text_to_embed)
        except Exception as exc:
            if attempt == attempts:
                print(f"⚠️ Embedding unavailable after {attempts} attempts: {type(exc).__name__}: {exc}")
                return None
            sleep_seconds = attempt * 3
            print(f"⏳ Embedding attempt {attempt} failed; retrying in {sleep_seconds}s")
            time.sleep(sleep_seconds)


def ensure_products_table() -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {VECTOR_TABLE_NAME} (
              id VARCHAR(255) PRIMARY KEY,
              name VARCHAR(255) NOT NULL,
              categories JSONB,
              description TEXT,
              price DECIMAL(10,2),
              image_url TEXT,
              product_embedding vector({VECTOR_DIMENSION}),
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS {VECTOR_TABLE_NAME}_embedding_idx
              ON {VECTOR_TABLE_NAME}
              USING ivfflat (product_embedding vector_cosine_ops)
              WITH (lists = 100)
        """))


def seed_products_if_empty() -> None:
    if not os.path.exists(PRODUCTS_JSON_PATH):
        print(f"⚠️ Product seed file not found at {PRODUCTS_JSON_PATH}; skipping seed")
        return

    with engine.begin() as conn:
        product_count = conn.execute(text(f"SELECT COUNT(*) FROM {VECTOR_TABLE_NAME}")).scalar_one()
        if product_count:
            print(f"📦 Product table already contains {product_count} rows")
            return

    with open(PRODUCTS_JSON_PATH, "r", encoding="utf-8") as product_file:
        products = json.load(product_file).get("products", [])

    print(f"🌱 Seeding {len(products)} products into RDS")
    with engine.begin() as conn:
        for product in products:
            description = product.get("description", "")
            categories = product.get("categories", [])
            embedding = None
            if SEED_EMBEDDINGS:
                embedding_text = (
                    f"{product.get('name', '')}. {description}. "
                    f"Categories: {', '.join(categories)}"
                )
                embedding = embed_with_retry(embedding_text)
            conn.execute(
                text(f"""
                    INSERT INTO {VECTOR_TABLE_NAME}
                      (id, name, categories, description, price, image_url, product_embedding)
                    VALUES
                      (:id, :name, CAST(:categories AS jsonb), :description, :price, :image_url, CAST(:embedding AS vector))
                    ON CONFLICT (id) DO UPDATE SET
                      name = EXCLUDED.name,
                      categories = EXCLUDED.categories,
                      description = EXCLUDED.description,
                      price = EXCLUDED.price,
                      image_url = EXCLUDED.image_url,
                      product_embedding = EXCLUDED.product_embedding
                """),
                {
                    "id": product["id"],
                    "name": product["name"],
                    "categories": json.dumps(categories),
                    "description": description,
                    "price": price_to_decimal(product.get("priceUsd", {})),
                    "image_url": product.get("picture"),
                    "embedding": json.dumps(embedding) if embedding else None,
                },
            )


def keyword_search_products(query: str, limit: int = 10) -> list[dict]:
    terms = [term for term in query.replace("\n", " ").split(" ") if len(term) > 2][:8]
    patterns = [f"%{term}%" for term in terms] or ["%%"]
    where_clause = " OR ".join(
        [f"name ILIKE :term_{index} OR description ILIKE :term_{index}" for index in range(len(patterns))]
    )
    params = {f"term_{index}": pattern for index, pattern in enumerate(patterns)}
    params["limit"] = limit
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT id, name, categories, description, image_url, price
                FROM {VECTOR_TABLE_NAME}
                WHERE {where_clause}
                LIMIT :limit
            """),
            params,
        ).mappings().all()
    return [dict(row) for row in rows]


def similarity_search_products(query: str, limit: int = 10) -> list[dict]:
    query_embedding = embed_with_retry(query, attempts=2)
    if not query_embedding:
        return keyword_search_products(query, limit)

    with engine.connect() as conn:
        embedded_count = conn.execute(
            text(f"SELECT COUNT(*) FROM {VECTOR_TABLE_NAME} WHERE product_embedding IS NOT NULL")
        ).scalar_one()
        if embedded_count == 0:
            return keyword_search_products(query, limit)

        rows = conn.execute(
            text(f"""
                SELECT id, name, categories, description, image_url, price,
                       product_embedding <=> CAST(:embedding AS vector) AS distance
                FROM {VECTOR_TABLE_NAME}
                WHERE product_embedding IS NOT NULL
                ORDER BY product_embedding <=> CAST(:embedding AS vector)
                LIMIT :limit
            """),
            {"embedding": json.dumps(query_embedding), "limit": limit},
        ).mappings().all()
    return [dict(row) for row in rows]


def format_fallback_recommendation(prompt: str, docs: list[dict], reason: Exception) -> str:
    if not docs:
        return (
            "I could not find a matching product in the current catalog. "
            f"Bedrock generation is temporarily unavailable: {type(reason).__name__}."
        )

    recommendations = []
    for doc in docs[:3]:
        recommendations.append(
            f"{doc.get('name')} ({doc.get('id')}): {doc.get('description')}"
        )
    ids = ", ".join(f"[{doc.get('id')}]" for doc in docs[:3])
    return (
        f"For your request, \"{prompt}\", the closest catalog matches are: "
        + "; ".join(recommendations)
        + f". Top product IDs: {ids}. "
        + f"Bedrock generation is temporarily unavailable: {type(reason).__name__}."
    )


ensure_products_table()
seed_products_if_empty()


def create_app():
    app = Flask(__name__)

    @app.route("/", methods=['POST'])
    def talkToBedrock():
        try:
            print("🔄 Beginning RAG call")
            
            # Parse request
            if not request.is_json:
                return {'error': 'Content-Type must be application/json'}, 400
                
            prompt = request.json.get('message', '')
            prompt = unquote(prompt)
            image_data = request.json.get('image')  # Optional base64 image

            if not prompt:
                return {'error': 'Missing "message" in request'}, 400

            # Step 1 – Get room description from Bedrock (Claude 3 Sonnet)
            llm_vision = ChatBedrock(
                model_id="anthropic.claude-3-sonnet-20240229-v1:0",
                region_name=AWS_REGION,
                model_kwargs={"temperature": 0.1, "max_tokens": 1024}
            )
            
            # Build message content (text-only fallback; vision requires extra handling)
            if image_data:
                # For production vision: decode base64 + format as Claude 3 multimodal block
                # For now, use text-only to avoid complexity
                message_content = f"You are a professional interior designer. A customer is asking: {prompt}. Describe what style of room they might be referring to and what products would complement it."
            else:
                message_content = f"You are a professional interior designer. A customer is asking: {prompt}. Describe what style of room they might be referring to and what products would complement it."
            
            try:
                message = HumanMessage(content=message_content)
                response = llm_vision.invoke([message])
                print(f"📝 Description step: {response.content[:200]}...")
                description_response = response.content
            except Exception as e:
                print(f"⚠️ Bedrock description step unavailable: {type(e).__name__}: {e}")
                description_response = prompt

            # Step 2 – Similarity search with description + user prompt
            vector_search_prompt = f"""
            User request: {prompt}
            Room style context: {description_response}
            Find the most relevant products that match both the request and the room style.
            """
            
            docs = similarity_search_products(vector_search_prompt, limit=10)
            print(f"🔍 Retrieved {len(docs)} documents from vector store")
            
            # Prepare relevant documents for final prompt
            relevant_docs = ""
            for i, doc in enumerate(docs[:5]):  # Limit to top 5 for context window
                relevant_docs += (
                    f"{i+1}. {doc.get('name')}: {doc.get('description')} "
                    f"[ID: {doc.get('id')}, Categories: {doc.get('categories')}], "
                )

            # Step 3 – Final recommendation generation with Bedrock
            llm = ChatBedrock(
                model_id=BEDROCK_MODEL_ID,
                region_name=AWS_REGION,
                model_kwargs={"temperature": 0.2, "max_tokens": 2048}
            )
            
            design_prompt = (
                f"You are an interior designer for Online Boutique. Help a customer choose products for their room.\n\n"
                f"ROOM DESCRIPTION: {description_response}\n\n"
                f"AVAILABLE PRODUCTS: {relevant_docs}\n\n"
                f"CUSTOMER REQUEST: {prompt}\n\n"
                f"INSTRUCTIONS:\n"
                f"1. Briefly acknowledge the room style.\n"
                f"2. Recommend 1-3 products from the available list that best match the request.\n"
                f"3. If no products match, politely say so — do NOT invent products.\n"
                f"4. End with product IDs in this format for top recommendations: [ID1], [ID2], [ID3]\n\n"
                f"Your response:"
            )
            
            print(f"🎯 Final prompt length: {len(design_prompt)} chars")
            try:
                design_response = llm.invoke(design_prompt)
                result = {'content': design_response.content}
            except Exception as e:
                print(f"⚠️ Bedrock final step unavailable: {type(e).__name__}: {e}")
                result = {'content': format_fallback_recommendation(prompt, docs, e)}
            
            print("✅ RAG call completed successfully")
            return result
            
        except Exception as e:
            print(f"❌ Error in talkToBedrock: {type(e).__name__}: {e}")
            return {'error': f'Internal server error: {str(e)}'}, 500

    @app.route("/health", methods=['GET'])
    def health_check():
        """Simple health endpoint for load balancers"""
        try:
            # Test DB connection
            with engine.connect() as conn:
                product_count = conn.execute(text(f"SELECT COUNT(*) FROM {VECTOR_TABLE_NAME}")).scalar_one()
            if product_count == 0:
                return {'status': 'unhealthy', 'error': 'products table is empty'}, 503
            return {'status': 'healthy', 'service': 'shopping-assistant'}, 200
        except Exception as e:
            return {'status': 'unhealthy', 'error': str(e)}, 503

    return app


if __name__ == "__main__":
    print(f"🚀 Starting Shopping Assistant Service on port {os.environ.get('PORT', 8080)}")
    print(f"🌍 Region: {AWS_REGION} | 🗄️ RDS: {RDS_HOST} | 🤖 Model: {BEDROCK_MODEL_ID}")
    
    app = create_app()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)), debug=False)
