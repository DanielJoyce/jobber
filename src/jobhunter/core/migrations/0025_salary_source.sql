-- 0025_salary_source: where the salary columns came from (specs/003 "Salary from description
-- text"). 'structured' = the source's own pay field; 'text' = extracted from the description
-- by normalize (salary_raw then holds the matched snippet). NULL = no salary.
ALTER TABLE job ADD COLUMN salary_source TEXT CHECK (salary_source IN ('structured', 'text'));
UPDATE job SET salary_source = 'structured' WHERE salary_stated = 1;
