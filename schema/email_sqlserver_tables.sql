IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'asi')
    EXEC(N'CREATE SCHEMA asi');
GO

IF OBJECT_ID(N'asi.email_account', N'U') IS NULL
BEGIN
    CREATE TABLE asi.email_account (
        id BIGINT IDENTITY(1,1) PRIMARY KEY,
        tenant_key NVARCHAR(100) NOT NULL,
        app_id NVARCHAR(100) NOT NULL,
        bot_code NVARCHAR(100) NULL,
        open_id NVARCHAR(100) NOT NULL,
        chat_id NVARCHAR(100) NOT NULL,
        email_address NVARCHAR(320) NOT NULL,
        password_ciphertext NVARCHAR(MAX) NOT NULL,
        pop3_host NVARCHAR(255) NOT NULL,
        pop3_port INT NOT NULL,
        retention_days INT NOT NULL CONSTRAINT DF_email_account_retention DEFAULT 7,
        enabled BIT NOT NULL CONSTRAINT DF_email_account_enabled DEFAULT 1,
        last_sync_at DATETIME2 NULL,
        last_sync_error NVARCHAR(1000) NULL,
        created_at DATETIME2 NOT NULL CONSTRAINT DF_email_account_created DEFAULT SYSUTCDATETIME(),
        updated_at DATETIME2 NOT NULL CONSTRAINT DF_email_account_updated DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_email_account_identity UNIQUE (tenant_key, app_id, open_id)
    );
END;
GO

IF OBJECT_ID(N'asi.email_message', N'U') IS NULL
BEGIN
    CREATE TABLE asi.email_message (
        id BIGINT IDENTITY(1,1) PRIMARY KEY,
        email_account_id BIGINT NOT NULL,
        pop3_uidl NVARCHAR(255) NOT NULL,
        message_id NVARCHAR(1000) NULL,
        references_header NVARCHAR(MAX) NULL,
        in_reply_to NVARCHAR(1000) NULL,
        subject NVARCHAR(1000) NULL,
        sender_name NVARCHAR(500) NULL,
        sender_address NVARCHAR(320) NULL,
        to_json NVARCHAR(MAX) NOT NULL,
        cc_json NVARCHAR(MAX) NOT NULL,
        sent_at DATETIME2 NULL,
        received_at DATETIME2 NOT NULL CONSTRAINT DF_email_message_received DEFAULT SYSUTCDATETIME(),
        text_body NVARCHAR(MAX) NULL,
        html_body NVARCHAR(MAX) NULL,
        attachments_json NVARCHAR(MAX) NOT NULL,
        sync_status NVARCHAR(30) NOT NULL CONSTRAINT DF_email_message_sync DEFAULT N'success',
        analysis_status NVARCHAR(30) NOT NULL CONSTRAINT DF_email_message_analysis DEFAULT N'pending',
        push_status NVARCHAR(30) NOT NULL CONSTRAINT DF_email_message_push DEFAULT N'pending',
        analysis_failed_reason NVARCHAR(1000) NULL,
        push_failed_reason NVARCHAR(1000) NULL,
        created_at DATETIME2 NOT NULL CONSTRAINT DF_email_message_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_email_message_account FOREIGN KEY (email_account_id) REFERENCES asi.email_account(id),
        CONSTRAINT UQ_email_message_uidl UNIQUE (email_account_id, pop3_uidl)
    );
    CREATE INDEX IX_email_message_account_time
        ON asi.email_message (email_account_id, sent_at DESC, id DESC);
END;
GO

IF OBJECT_ID(N'asi.email_analysis', N'U') IS NULL
BEGIN
    CREATE TABLE asi.email_analysis (
        id BIGINT IDENTITY(1,1) PRIMARY KEY,
        email_message_id BIGINT NOT NULL,
        summary NVARCHAR(MAX) NOT NULL,
        importance NVARCHAR(20) NOT NULL,
        requires_attention BIT NOT NULL,
        relation_type NVARCHAR(30) NOT NULL,
        todos_json NVARCHAR(MAX) NOT NULL,
        possible_owner NVARCHAR(500) NULL,
        deadline NVARCHAR(500) NULL,
        risks_json NVARCHAR(MAX) NOT NULL,
        model_name NVARCHAR(200) NULL,
        created_at DATETIME2 NOT NULL CONSTRAINT DF_email_analysis_created DEFAULT SYSUTCDATETIME(),
        expires_at DATETIME2 NOT NULL,
        CONSTRAINT FK_email_analysis_message FOREIGN KEY (email_message_id) REFERENCES asi.email_message(id),
        CONSTRAINT UQ_email_analysis_message UNIQUE (email_message_id)
    );
    CREATE INDEX IX_email_analysis_expiry ON asi.email_analysis (expires_at);
END;
GO

IF OBJECT_ID(N'asi.email_push_log', N'U') IS NULL
BEGIN
    CREATE TABLE asi.email_push_log (
        id BIGINT IDENTITY(1,1) PRIMARY KEY,
        email_message_id BIGINT NOT NULL,
        push_type NVARCHAR(30) NOT NULL,
        task_ref NVARCHAR(100) NOT NULL CONSTRAINT DF_email_push_task DEFAULT N'',
        run_ref NVARCHAR(100) NOT NULL,
        status NVARCHAR(30) NOT NULL,
        error NVARCHAR(1000) NULL,
        pushed_at DATETIME2 NULL,
        created_at DATETIME2 NOT NULL CONSTRAINT DF_email_push_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_email_push_message FOREIGN KEY (email_message_id) REFERENCES asi.email_message(id),
        CONSTRAINT UQ_email_push_once UNIQUE (email_message_id, push_type, task_ref, run_ref)
    );
    CREATE INDEX IX_email_push_run ON asi.email_push_log (run_ref, status);
END;
GO
