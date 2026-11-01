-- 给 torrents 表加 pieces_hash（info.pieces 段的 SHA1，40 位十六进制）
--
-- 为什么是「pieces 段的 SHA1」而不是 info_hash：同一份文件在不同站点的种子，info_hash 各不相同
-- （tracker、发布者、creation date 都会变），但 info.pieces 完全一致 —— 那一段是文件内容本身。
-- 只要两边算的是同一份文件的 pieces，就有唯一答案，且不可能撞车。

ALTER TABLE torrents
    ADD COLUMN pieces_hash CHAR(40) NULL DEFAULT NULL COMMENT 'info.pieces 的 SHA1，用于跨站辅种匹配';

-- 查询是 WHERE pieces_hash IN (...)，索引必须建
ALTER TABLE torrents
    ADD INDEX idx_torrents_pieces_hash (pieces_hash);
