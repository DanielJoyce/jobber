# 010 — Source inventory

**54 sources** (50 states + DC, Guam, Northern Mariana Islands, US Virgin Islands), extracted
from [CareerOneStop's state job banks page](https://www.careeronestop.org/JobSearch/FindJobs/state-job-banks.aspx)
and probed on **2026-10-09**. Plus USAJOBS as source class C ([011](011-federal-and-geography.md)).

Every URL below returned HTTP 200 on a live request unless noted. `registry.yaml` is generated
from this table — this document is the human-editable record of what was observed and when.

## Family distribution

| Family | Sources |
|---|---|
| `vos` | 25 |
| `?` | 18 |
| `joblink` | 7 |
| `nlx` | 3 |
| `next/custom` | 1 |
| `usajobs` (class C) | 1 |

`?` means the entry URL is verified reachable but the platform is not yet classified.
Classifying those is [Milestone 1](009-roadmap.md); the expectation is that most resolve into
`vos` or `joblink` once probed with a warmed session rather than a bare GET.

## Evidence behind the family calls

- **`vos`** — Geographic Solutions "Virtual OneStop". ASP.NET WebForms. 24 sources carry
  `/vosnet/` in the CareerOneStop URL; Connecticut redirects to it and returned all five
  signals (`vosnet`, `VOSNet`, `Geographic Solutions`, `Virtual OneStop`, `__VIEWSTATE`).
- **`joblink`** — one Rails/Webpacker codebase deployed per state. Fetching
  `/packs/js/4743-860615d0fe49e3040bc6.js` from all seven hosts returned the **byte-identical
  sha256** (`2c0d5d29e49fd986…`), as did `translate_config-d06f51c2…js`. `GET /ada/r/search/jobs`
  returned **401 with a 26-byte body** — a real JSON API behind a session, not a missing route.
- **`nlx`** — National Labor Exchange (NASWA / DirectEmployers) aggregator.
- **`next/custom`** — Next.js; `__NEXT_DATA__` plus an `/api/` surface.

## The table

| ST | State / Territory | Job bank | Entry URL | Family | Evidence |
|---|---|---|---|---|---|
| AL | Alabama | Alabama JobLink | `https://alabamaworks.alabama.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| AK | Alaska | Alaska Job Center Network | `https://alaskajobs.alaska.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| AZ | Arizona | Arizona Workforce Connection | `https://www.azjobconnection.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` |
| AR | Arkansas | Arkansas JobLink | `https://www.arjoblink.arkansas.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` |
| CA | California | California' Caljobs | `https://www.caljobs.ca.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| CO | Colorado | Connecting Colorado | `https://www.connectingcolorado.com/` | `?` | unclassified — URL verified reachable |
| CT | Connecticut | Job and Career ConneCTion. | `https://www.cthires.com/` | `vos` | **verified** — 302 → `/vosnet/default.aspx`, 5 signals |
| DE | Delaware | Delaware Job Link | `https://joblink.delaware.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` |
| DC | District of Columbia | DCNetworks | `https://www.dcnetworks.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| FL | Florida | Employ Florida Marketplace | `https://www.employflorida.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| GA | Georgia | WorkSource Georgia | `https://www.worksourcegaportal.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| GU | Guam | Guam Department of Labor - Job Bank | `https://www.hireguam.com/vosnet/default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| HI | Hawaii | HireNet Hawaii | `https://www.hirenethawaii.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| ID | Idaho | Idaho Commerce and Labor Job Search | `https://idahoworks.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` |
| IL | Illinois | Illinois Department of Employment Security | `https://illinoisjoblink.illinois.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` |
| IN | Indiana | Indiana Workforce Development | `https://www.indianacareerconnect.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| IA | Iowa | IowaJobs | `https://www.iowaworks.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| KS | Kansas | KansasWorks | `https://www.kansasworks.com/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` |
| KY | Kentucky | EmployKy | `https://kyjobs.usnlx.com/jobs/` | `nlx` | **verified** — `usnlx` strings |
| LA | Louisiana | Louisiana Works | `https://www.louisianaworks.net/hire/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| ME | Maine | Maine Employment Info Guide | `https://joblink.maine.gov/` | `joblink` | **verified** — identical bundle sha `2c0d5d29…` |
| MD | Maryland | Maryland Workforce Exchange | `https://mwejobs.maryland.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| MA | Massachusetts | Massachusetts JobQuest | `https://jobquest.mass.gov/` | `?` | unclassified — URL verified reachable |
| MI | Michigan | Michigan's Talent Bank | `https://www.mitalent.org/` | `?` | unclassified — URL verified reachable |
| MN | Minnesota | Minnesota's Job Bank - Where Job Seekers and Employers Click | `https://careerforce.mn.gov/` | `?` | unclassified — URL verified reachable |
| MS | Mississippi | Increasing Employment in Mississippi | `https://wings.mdes.ms.gov/wings/welcome.jsp` | `?` | unclassified — URL verified reachable |
| MO | Missouri | GreatHires - Missouri's Workforce Resource | `https://jobs.mo.gov/` | `?` | unclassified — URL verified reachable |
| MT | Montana | Jobs - Montana | `https://montanaworks.gov/` | `nlx` | **verified** — `usnlx` strings |
| NE | Nebraska | NEworks | `https://neworks.nebraska.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| NV | Nevada |  | `https://www.employnv.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| NH | New Hampshire |  | `https://nhworksjobmatch.nhes.nh.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| NJ | New Jersey | New Jersey Department of Labor and Workforce Development | `https://nj.gov/labor/career-services/` | `?` | unclassified — URL verified reachable |
| NM | New Mexico | New Mexico Workforce Connection | `https://www.jobs.dws.nm.gov/` | `?` | unclassified — URL verified reachable |
| NY | New York | Workforce New York | `https://newyork.usnlx.com/index.asp` | `nlx` | **verified** — `usnlx` strings |
| NC | North Carolina | The Employment Security Commission of North Carolina | `https://www.ncworks.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| ND | North Dakota | JobService North Dakota - Your Workforce Connection | `https://www.ndworkforceconnection.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| MP | Northern Mariana Islands |  | `https://marianaslabor.net/jvapub_list.asp` | `?` | unclassified — URL verified reachable |
| OH | Ohio | Ohio's Statewide Job Matching System | `https://ohiomeansjobs.ohio.gov/home` | `?` | unclassified — URL verified reachable |
| OK | Oklahoma | Oklahoma Job Link | `https://www.employoklahoma.gov/` | `?` | unclassified — URL verified reachable |
| OR | Oregon | Find work, post jobs and locate workforce services | `https://secure.emp.state.or.us/jobs/index.cfm?bkmk=w9` | `?` | unclassified — URL verified reachable |
| PA | Pennsylvania | Services for Job Seekers and Employers | `https://www.pacareerlink.pa.gov/jponline/Common/LandingPage/` | `?` | unclassified — URL verified reachable |
| RI | Rhode Island | Rhode Island Job Bank | `https://www.employri.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| SC | South Carolina | South Carolina Job Bank | `https://jobs.scworks.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| SD | South Dakota | South Dakota Department of Labor and Regulation | `https://www.southdakotaworks.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| TN | Tennessee | Tennessee Department of Labor and Workforce Development | `https://www.jobs4tn.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| TX | Texas | WorkInTexas | `https://www.workintexas.com/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| UT | Utah | Utah's Job Connection | `https://jobs.utah.gov/index.html` | `?` | unclassified — URL verified reachable |
| VT | Vermont | Vermont JobLink | `https://www.vermontjoblink.com/` | `?` | unclassified — URL verified reachable |
| VI | Virgin Islands | Virgin Islands Job Bank | `https://www.vidolviews.org/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| VA | Virginia | Virginia's Automated Labor Exchange | `https://vawc.virginia.gov/vosnet/Default.aspx` | `vos` | **verified** — `/vosnet/` in URL |
| WA | Washington |  | `https://worksourcewa.com/home.aspx` | `?` | unclassified — URL verified reachable |
| WV | West Virginia | WorkForce West Virginia | `https://workforcewv.org/individuals/` | `?` | unclassified — URL verified reachable |
| WI | Wisconsin | Wisconsin Job Center | `https://jobcenterofwisconsin.com/` | `?` | unclassified — URL verified reachable |
| WY | Wyoming | Wyoming at Work | `https://hire.wyo.gov/home` | `next/custom` | **verified** — `__NEXT_DATA__`, `/api/workflow` |

## Not in this list

- **Puerto Rico** — absent from the CareerOneStop page. Worth adding manually if wanted.
- **Class B (state-employer career sites)** — a separate inventory, not yet built. The six rows
  probed while drafting [003](003-sources-and-adapters.md) are recorded there, including the
  finding that `governmentjobs.com` disallows generic crawlers ([008](008-compliance.md)).
