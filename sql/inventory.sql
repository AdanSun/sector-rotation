-- financedata / Wind 落库数据盘点
-- 本文件仅查询 information_schema，不修改数据库。
-- 执行前可先运行：USE financedata;

-- 1. 数据库中的全部基础表与视图
SELECT
    t.TABLE_NAME,
    t.TABLE_TYPE,
    t.ENGINE,
    t.TABLE_ROWS,
    t.CREATE_TIME,
    t.UPDATE_TIME
FROM information_schema.TABLES AS t
WHERE t.TABLE_SCHEMA = 'financedata'
ORDER BY t.TABLE_NAME;


-- 2. 筛选本研报可能需要的 Wind 表
SELECT
    t.TABLE_NAME,
    t.TABLE_TYPE,
    t.TABLE_ROWS
FROM information_schema.TABLES AS t
WHERE t.TABLE_SCHEMA = 'financedata'
  AND (
       LOWER(t.TABLE_NAME) LIKE '%ashareeodprice%'
    OR LOWER(t.TABLE_NAME) LIKE '%derivative%'
    OR LOWER(t.TABLE_NAME) LIKE '%financialindicator%'
    OR LOWER(t.TABLE_NAME) LIKE '%income%'
    OR LOWER(t.TABLE_NAME) LIKE '%balance%'
    OR LOWER(t.TABLE_NAME) LIKE '%cashflow%'
    OR LOWER(t.TABLE_NAME) LIKE '%description%'
    OR LOWER(t.TABLE_NAME) LIKE '%industr%'
    OR LOWER(t.TABLE_NAME) LIKE '%citic%'
    OR LOWER(t.TABLE_NAME) LIKE '%consensus%'
    OR LOWER(t.TABLE_NAME) LIKE '%forecast%'
    OR LOWER(t.TABLE_NAME) LIKE '%earning%'
    OR LOWER(t.TABLE_NAME) LIKE '%dividend%'
    OR LOWER(t.TABLE_NAME) LIKE '%indexeod%'
    OR LOWER(t.TABLE_NAME) LIKE '%calendar%'
    OR LOWER(t.TABLE_NAME) LIKE '%st%'
    OR LOWER(t.TABLE_NAME) LIKE '%suspend%'
  )
ORDER BY t.TABLE_NAME;


-- 3. 上述候选表的全部字段
SELECT
    c.TABLE_NAME,
    c.ORDINAL_POSITION,
    c.COLUMN_NAME,
    c.COLUMN_TYPE,
    c.IS_NULLABLE,
    c.COLUMN_KEY,
    c.COLUMN_DEFAULT,
    c.COLUMN_COMMENT
FROM information_schema.COLUMNS AS c
WHERE c.TABLE_SCHEMA = 'financedata'
  AND (
       LOWER(c.TABLE_NAME) LIKE '%ashareeodprice%'
    OR LOWER(c.TABLE_NAME) LIKE '%derivative%'
    OR LOWER(c.TABLE_NAME) LIKE '%financialindicator%'
    OR LOWER(c.TABLE_NAME) LIKE '%income%'
    OR LOWER(c.TABLE_NAME) LIKE '%balance%'
    OR LOWER(c.TABLE_NAME) LIKE '%cashflow%'
    OR LOWER(c.TABLE_NAME) LIKE '%description%'
    OR LOWER(c.TABLE_NAME) LIKE '%industr%'
    OR LOWER(c.TABLE_NAME) LIKE '%citic%'
    OR LOWER(c.TABLE_NAME) LIKE '%consensus%'
    OR LOWER(c.TABLE_NAME) LIKE '%forecast%'
    OR LOWER(c.TABLE_NAME) LIKE '%earning%'
    OR LOWER(c.TABLE_NAME) LIKE '%dividend%'
    OR LOWER(c.TABLE_NAME) LIKE '%indexeod%'
    OR LOWER(c.TABLE_NAME) LIKE '%calendar%'
    OR LOWER(c.TABLE_NAME) LIKE '%st%'
    OR LOWER(c.TABLE_NAME) LIKE '%suspend%'
  )
ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION;


-- 4. 搜索关键字段所在表，用来建立“研报指标 -> Wind 字段”映射
SELECT
    c.TABLE_NAME,
    c.COLUMN_NAME,
    c.COLUMN_TYPE,
    c.COLUMN_COMMENT
FROM information_schema.COLUMNS AS c
WHERE c.TABLE_SCHEMA = 'financedata'
  AND (
       UPPER(c.COLUMN_NAME) IN (
           'S_INFO_WINDCODE',
           'TRADE_DT',
           'ANN_DT',
           'REPORT_PERIOD',
           'STATEMENT_TYPE',
           'S_INFO_COMPCODE'
       )
    OR LOWER(c.COLUMN_NAME) LIKE '%operate%cash%flow%'
    OR LOWER(c.COLUMN_NAME) LIKE '%invest%cash%flow%'
    OR LOWER(c.COLUMN_NAME) LIKE '%financ%cash%flow%'
    OR LOWER(c.COLUMN_NAME) LIKE '%net%profit%'
    OR LOWER(c.COLUMN_NAME) LIKE '%tot%shrhldr%eqy%'
    OR LOWER(c.COLUMN_NAME) LIKE '%roe%'
    OR LOWER(c.COLUMN_NAME) LIKE '%pe%ttm%'
    OR LOWER(c.COLUMN_NAME) LIKE '%pb%'
    OR LOWER(c.COLUMN_NAME) LIKE '%divid%'
    OR LOWER(c.COLUMN_NAME) LIKE '%float%mv%'
    OR LOWER(c.COLUMN_NAME) LIKE '%consensus%'
    OR LOWER(c.COLUMN_NAME) LIKE '%industry%'
    OR LOWER(c.COLUMN_NAME) LIKE '%entry%dt%'
    OR LOWER(c.COLUMN_NAME) LIKE '%remove%dt%'
  )
ORDER BY c.COLUMN_NAME, c.TABLE_NAME;


-- 5. 查看候选表的索引，判断后续按股票和日期查询是否高效
SELECT
    s.TABLE_NAME,
    s.INDEX_NAME,
    s.SEQ_IN_INDEX,
    s.COLUMN_NAME,
    s.NON_UNIQUE
FROM information_schema.STATISTICS AS s
WHERE s.TABLE_SCHEMA = 'financedata'
  AND (
       LOWER(s.TABLE_NAME) LIKE '%ashareeodprice%'
    OR LOWER(s.TABLE_NAME) LIKE '%financialindicator%'
    OR LOWER(s.TABLE_NAME) LIKE '%income%'
    OR LOWER(s.TABLE_NAME) LIKE '%balance%'
    OR LOWER(s.TABLE_NAME) LIKE '%cashflow%'
    OR LOWER(s.TABLE_NAME) LIKE '%industr%'
    OR LOWER(s.TABLE_NAME) LIKE '%consensus%'
  )
ORDER BY s.TABLE_NAME, s.INDEX_NAME, s.SEQ_IN_INDEX;
