#!/bin/bash
set -euo pipefail

# ===== CONFIG =====
REGION="ap-south-1"
DB_ID="shoppingassistant-db"
DB_NAME="shoppingassistant"
DB_USER="postgres"
SECRET="prod/shoppingassistant/db"
VECTOR_DIM=1024
PG_VER="16.6"
SUBNET_GRP="shoppingassistant-db-subnet-group"

log() { echo "[$(date +%H:%M:%S)] $1"; }

# ===== PASSWORD: Get existing or generate new =====
if aws secretsmanager get-secret-value --secret-id "$SECRET" --region "$REGION" &>/dev/null; then
  log "🔐 Using existing password from Secrets Manager"
  DB_PASS=$(aws secretsmanager get-secret-value --secret-id "$SECRET" --query "SecretString" --output text --region "$REGION" | jq -r .password)
else
  log "🔐 Generating new password"
  DB_PASS="$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9!#$%^&*()-_=+' | head -c 20)A1!"
  log "⚠️ SAVE THIS PASSWORD: $DB_PASS"
fi

# ===== VPC & SUBNETS =====
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=is-default,Values=true" --query "Vpcs[0].VpcId" --output text --region "$REGION")
SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" --query "Subnets[0:2].SubnetId" --output text --region "$REGION" | tr '\t' ' ')
DEFAULT_SG=$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=default" --query "SecurityGroups[0].GroupId" --output text --region "$REGION")
VPC_CIDR=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --query "Vpcs[0].CidrBlock" --output text)

# Allow PostgreSQL from VPC
aws ec2 authorize-security-group-ingress --group-id "$DEFAULT_SG" --protocol tcp --port 5432 --cidr "$VPC_CIDR" --region "$REGION" 2>/dev/null || true

# ===== SUBNET GROUP =====
if ! aws rds describe-db-subnet-groups --db-subnet-group-name "$SUBNET_GRP" --region "$REGION" &>/dev/null; then
  log "🔧 Creating subnet group"
  aws rds create-db-subnet-group --db-subnet-group-name "$SUBNET_GRP" --db-subnet-group-description "Shopping Assistant" --subnet-ids $SUBNETS --region "$REGION"
fi

# ===== RDS INSTANCE =====
if aws rds describe-db-instances --db-instance-identifier "$DB_ID" --region "$REGION" &>/dev/null; then
  log "⏭️ RDS exists, checking password..."
  RDS_HOST=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" --query "DBInstances[0].Endpoint.Address" --output text --region "$REGION")
  
  # Test connection; if fails, reset password
  if ! PGPASSWORD="$DB_PASS" psql "host=$RDS_HOST user=$DB_USER dbname=postgres sslmode=require" -c "SELECT 1" &>/dev/null; then
    log "🔄 Password mismatch detected. Resetting RDS password..."
    aws rds modify-db-instance --db-instance-identifier "$DB_ID" --master-user-password "$DB_PASS" --apply-immediately --region "$REGION"
    sleep 60
    aws rds wait db-instance-available --db-instance-identifier "$DB_ID" --region "$REGION"
  fi
else
  log "🗄️ Creating RDS instance (this takes ~5-10 mins)..."
  aws rds create-db-instance \
    --db-instance-identifier "$DB_ID" \
    --db-instance-class db.t4g.micro \
    --engine postgres --engine-version "$PG_VER" \
    --master-username "$DB_USER" --master-user-password "$DB_PASS" \
    --allocated-storage 20 --storage-type gp3 \
    --db-subnet-group-name "$SUBNET_GRP" \
    --vpc-security-group-ids "$DEFAULT_SG" \
    --backup-retention-period 0 --no-multi-az --no-publicly-accessible \
    --region "$REGION"
  aws rds wait db-instance-available --db-instance-identifier "$DB_ID" --region "$REGION"
  RDS_HOST=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" --query "DBInstances[0].Endpoint.Address" --output text --region "$REGION")
fi

log "✅ RDS Endpoint: $RDS_HOST"

# ===== DATABASE SETUP (with SSL) =====
log "🔌 Setting up pgvector and schema..."
sleep 5

# Enable extension + create DB
PGPASSWORD="$DB_PASS" psql "host=$RDS_HOST user=$DB_USER dbname=postgres sslmode=require" -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null || true
PGPASSWORD="$DB_PASS" psql "host=$RDS_HOST user=$DB_USER dbname=postgres sslmode=require" -c "CREATE DATABASE \"$DB_NAME\";" 2>/dev/null || true

# Create schema
PGPASSWORD="$DB_PASS" psql "host=$RDS_HOST user=$DB_USER dbname=$DB_NAME sslmode=require" <<EOSQL
CREATE TABLE IF NOT EXISTS products (
  id VARCHAR(255) PRIMARY KEY, name VARCHAR(255) NOT NULL,
  categories JSONB, description TEXT, price DECIMAL(10,2),
  image_url TEXT, product_embedding vector($VECTOR_DIM),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS products_embedding_idx 
ON products USING ivfflat (product_embedding vector_cosine_ops) WITH (lists = 100);
EOSQL

log "✅ pgvector and schema ready"

# ===== SECRETS MANAGER =====
SECRET_JSON="{\"username\":\"$DB_USER\",\"password\":\"$DB_PASS\",\"engine\":\"postgres\",\"host\":\"$RDS_HOST\",\"port\":5432,\"dbname\":\"$DB_NAME\"}"
if aws secretsmanager describe-secret --secret-id "$SECRET" --region "$REGION" &>/dev/null; then
  aws secretsmanager update-secret --secret-id "$SECRET" --secret-string "$SECRET_JSON" --region "$REGION" >/dev/null
else
  aws secretsmanager create-secret --name "$SECRET" --description "Shopping Assistant DB" --secret-string "$SECRET_JSON" --region "$REGION" >/dev/null
fi
log "✅ Credentials stored in Secrets Manager"

# ===== DONE =====
echo ""
echo "🎉 SUCCESS! Infrastructure ready."
echo ""
echo "📋 Container environment variables:"
echo "  AWS_REGION=$REGION"
echo "  RDS_HOST=$RDS_HOST"
echo "  RDS_DATABASE=$DB_NAME"
echo "  RDS_SECRET_NAME=$SECRET"
echo "  VECTOR_TABLE_NAME=products"
echo ""
echo "🚀 Deploy command:"
echo "  docker run -d -e AWS_REGION=$REGION -e RDS_HOST=$RDS_HOST -e RDS_DATABASE=$DB_NAME -e RDS_SECRET_NAME=$SECRET -e VECTOR_TABLE_NAME=products -p 8080:8080 --name shoppingassistantservice --restart unless-stopped shoppingassistantservice:aws-v1"
echo ""
echo "🔐 Password is safely stored in Secrets Manager: $SECRET"
