from .base import BaseCrawler
from .demo import DemoCrawler
from .real_sites import InThisWorkCrawler, JobKoreaCrawler, IncruitCrawler, ZighangCrawler
from .official_apis import SaraminApiCrawler, WantedApiCrawler

from .public_feeds import (GreenhousePublicCrawler, LeverPublicCrawler, AshbyPublicCrawler, SmartRecruitersPublicCrawler)

from .company_pages import CompanyJsonLdCrawler
from .approved_company_listings import ApprovedCompanyListingCrawler
from .domestic_ats import DomesticATSCrawler
from .aggregators import InThisWorkPublicCrawler, ZighangPublicCrawler

CRAWLERS: dict[str, type[BaseCrawler]] = {
    cls.site_id: cls for cls in (
        DemoCrawler, InThisWorkCrawler, SaraminApiCrawler, JobKoreaCrawler,
        GreenhousePublicCrawler, LeverPublicCrawler, AshbyPublicCrawler, SmartRecruitersPublicCrawler,
        IncruitCrawler, ZighangCrawler, WantedApiCrawler, CompanyJsonLdCrawler, ApprovedCompanyListingCrawler, DomesticATSCrawler,
    )
}
CRAWLERS.update(inthiswork=InThisWorkPublicCrawler,zighang=ZighangPublicCrawler)
