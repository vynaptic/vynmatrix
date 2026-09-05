SELECT t.table_name, COALESCE(s.n_live_tup, 0) as row_count
FROM information_schema.tables t
LEFT JOIN pg_stat_user_tables s ON t.table_name = s.relname
WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
ORDER BY COALESCE(s.n_live_tup, 0) DESC, t.table_name;
