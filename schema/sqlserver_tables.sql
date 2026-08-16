CREATE TABLE dbo.feishu_tenant_app (
  id BIGINT IDENTITY(1,1) PRIMARY KEY,
  tenant_key NVARCHAR(100) NOT NULL,
  app_id NVARCHAR(100) NOT NULL,
  app_secret NVARCHAR(500) NOT NULL,
  encrypt_key NVARCHAR(500) NULL,
  verification_token NVARCHAR(500) NULL,
  bot_code NVARCHAR(100) NOT NULL,
  bot_name NVARCHAR(200) NULL,
  enabled BIT NOT NULL CONSTRAINT DF_feishu_tenant_app_enabled DEFAULT 1,
  created_at DATETIME2 NOT NULL CONSTRAINT DF_feishu_tenant_app_created_at DEFAULT SYSUTCDATETIME(),
  CONSTRAINT UQ_feishu_tenant_app_tenant_app UNIQUE (tenant_key, app_id),
  CONSTRAINT UQ_feishu_tenant_app_bot_code UNIQUE (bot_code)
);

CREATE TABLE dbo.feishu_identity_mapping (
  id BIGINT IDENTITY(1,1) PRIMARY KEY,
  tenant_key NVARCHAR(100) NOT NULL,
  app_id NVARCHAR(100) NOT NULL,
  open_id NVARCHAR(100) NOT NULL,
  union_id NVARCHAR(100) NULL,
  user_id NVARCHAR(100) NULL,
  internal_username NVARCHAR(100) NULL,
  oa_user_id BIGINT NULL,
  display_name NVARCHAR(100) NULL,
  enabled BIT NOT NULL CONSTRAINT DF_feishu_identity_mapping_enabled DEFAULT 1,
  created_at DATETIME2 NOT NULL CONSTRAINT DF_feishu_identity_mapping_created_at DEFAULT SYSUTCDATETIME(),
  CONSTRAINT UQ_feishu_identity_mapping_user UNIQUE (tenant_key, app_id, open_id)
);

CREATE TABLE dbo.agent_audit_log (
  id BIGINT IDENTITY(1,1) PRIMARY KEY,
  request_id NVARCHAR(100) NOT NULL,
  tenant_key NVARCHAR(100) NOT NULL,
  app_id NVARCHAR(100) NOT NULL,
  open_id NVARCHAR(100) NOT NULL,
  internal_username NVARCHAR(100) NULL,
  tool_name NVARCHAR(200) NULL,
  permission_result NVARCHAR(50) NULL,
  user_message NVARCHAR(MAX) NULL,
  tool_args NVARCHAR(MAX) NULL,
  tool_result_summary NVARCHAR(MAX) NULL,
  created_at DATETIME2 NOT NULL CONSTRAINT DF_agent_audit_log_created_at DEFAULT SYSUTCDATETIME()
);

CREATE INDEX IX_agent_audit_log_identity_time
ON dbo.agent_audit_log (tenant_key, app_id, open_id, created_at DESC);
