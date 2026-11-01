<?php
/**
 * pieces-hash 接口的数据库配置。
 *
 * 复制成 pieces-hash.config.php 后填自己的库。建议单独建一个只有 SELECT 权限的账号：
 *   CREATE USER 'ph_ro'@'localhost' IDENTIFIED BY '...';
 *   GRANT SELECT ON `你的库`.`users` TO 'ph_ro'@'localhost';
 *   GRANT SELECT ON `你的库`.`torrents` TO 'ph_ro'@'localhost';
 *
 * NexusPHP 默认表名就是 users / torrents，pieces_hash 列由 pieces_hash.sql 添加。
 */
return [
    // MySQL：mysql:host=localhost;dbname=nexusphp;charset=utf8mb4
    'dsn'      => 'mysql:host=localhost;dbname=nexusphp;charset=utf8mb4',
    'user'     => 'ph_ro',
    'password' => '换成只读账号的密码',
    'table'    => 'torrents',
    'key_column' => 'pieces_hash',
];
