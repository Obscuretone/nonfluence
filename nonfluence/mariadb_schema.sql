CREATE TABLE workspace_metadata (
    singleton TINYINT UNSIGNED PRIMARY KEY CHECK (singleton = 1),
    schema_version TINYINT UNSIGNED NOT NULL,
    classification VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL
) ENGINE=InnoDB;

CREATE TABLE pages (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    space VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    title VARCHAR(160) NOT NULL,
    markdown MEDIUMTEXT NOT NULL,
    parent_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin,
    revision INT UNSIGNED NOT NULL,
    updated_at CHAR(24) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    KEY page_space_parent (space, parent_id),
    FOREIGN KEY (parent_id) REFERENCES pages(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE revisions (
    page_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    revision INT UNSIGNED NOT NULL,
    title VARCHAR(160) NOT NULL,
    markdown MEDIUMTEXT NOT NULL,
    parent_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin,
    updated_at CHAR(24) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    PRIMARY KEY (page_id, revision),
    FOREIGN KEY (page_id) REFERENCES pages(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE create_requests (
    space VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    page_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    PRIMARY KEY (space, request_id),
    FOREIGN KEY (page_id) REFERENCES pages(id)
) ENGINE=InnoDB;

CREATE TABLE page_references (
    rowid BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    kind VARCHAR(24) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    target_id MEDIUMTEXT NOT NULL,
    label MEDIUMTEXT NOT NULL,
    target_digest BINARY(32) AS (UNHEX(SHA2(target_id, 256))) PERSISTENT,
    UNIQUE KEY source_target (source_id, kind, target_digest),
    KEY reference_target (kind, target_id(255)),
    KEY reference_source (source_id, rowid),
    FOREIGN KEY (source_id) REFERENCES pages(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE page_terms (
    token BINARY(32) NOT NULL,
    page_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    title_hits INT UNSIGNED NOT NULL,
    body_hits INT UNSIGNED NOT NULL,
    PRIMARY KEY (token, page_id),
    KEY page_term_page (page_id),
    FOREIGN KEY (page_id) REFERENCES pages(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE owner_sessions (
    digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    owner VARCHAR(255) NOT NULL,
    expires BIGINT UNSIGNED NOT NULL,
    KEY session_expiry (expires)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
