-- 0026_salary_raw_original: the source's own salary_raw while salary_raw holds a snippet taken
-- from the description (salary_source = 'text'). A structured value with no numbers ("DOE",
-- "Competitive") is kept here so a later normalize can restore it; NULL otherwise.
ALTER TABLE job ADD COLUMN salary_raw_original TEXT;
