import pytest
from bs4 import BeautifulSoup

from app.collection import CollectionError, PublicFetcher
from app.website_agent import WebsitePlan, collect_with_plan, excerpt, inline_jobs


PLAN = WebsitePlan(job_row_selector='.jobs > li', job_link_selector='a.job',
    title_selector='.title', description_selector='.duty', location_selector='.city',
    complete_selector='.end')
HTML = '''<nav>导航</nav><main><ul class="jobs">
<li><a class="job" href="/position/one"><p class="title">算法工程师<span class="icon">热招</span></p>
<p class="city">上海</p><div class="duty">机器人算法研发</div></a></li>
<li><a class="job" href="/position/two"><p class="title">运维工程师</p>
<p class="city">杭州</p><div class="duty">建设基础设施</div></a></li>
</ul><p class="end">没有更多职位</p></main>'''


def test_inline_reads_each_job_without_detail_or_model(monkeypatch):
    urls = []
    def fetch(self, url):
        urls.append(url)
        return url, HTML
    monkeypatch.setattr(PublicFetcher, 'fetch', fetch)
    result = collect_with_plan({'entry_url': 'https://careers.example/jobs'},
                               {'max_pages': 10, 'max_details': 50}, PLAN)
    assert [d.title for d in result.records] == ['算法工程师', '运维工程师']
    assert result.records[0].source_job_id == 'https://careers.example/position/one'
    assert result.records[0].locations == ['上海']
    assert result.records[0].requirements == ''
    assert urls == ['https://careers.example/jobs']
    assert result.coverage == 'complete'


@pytest.mark.parametrize('html', [HTML.replace('/position/two','/position/one'),
                                  HTML.replace('class="duty"','class="changed"',1),
                                  HTML.replace('/position/one','https://other.example/job/1')])
def test_bad_rows_are_rejected_instead_of_collapsed_or_silently_skipped(html):
    with pytest.raises(CollectionError) as failure:
        inline_jobs(BeautifulSoup(html,'html.parser'), PLAN, 'https://careers.example/jobs')
    assert failure.value.code == 'selector_mismatch'


def test_budget_truncation_does_not_claim_complete(monkeypatch):
    monkeypatch.setattr(PublicFetcher,'fetch',lambda self,url:(url,HTML))
    result = collect_with_plan({'entry_url':'https://careers.example/jobs'},
                               {'max_pages':10,'max_details':1},PLAN)
    assert len(result.records) == 1 and result.coverage == 'partial'


def test_prompt_excerpt_prioritizes_actual_careers_main():
    html = '<div>' + '无关导航' * 3000 + '</div>' + HTML
    clean = excerpt(html)
    assert '机器人算法研发' in clean and '无关导航' not in clean


def test_anchor_can_itself_be_an_inline_job_row():
    plan = PLAN.model_copy(update={'job_row_selector': '.jobs > li > a.job'})
    docs = inline_jobs(BeautifulSoup(HTML, 'html.parser'), plan, 'https://careers.example/jobs')
    assert len(docs) == 2 and docs[0].title == '算法工程师'


def test_inline_checkpoint_resume_preserves_all_previously_saved_rows(db, monkeypatch):
    from app.models import Company, CompanyRun, SourceBinding, SourceRun, Subject, TaskRun, now
    from app.official_checkpoint import OfficialCheckpointStore
    from app.serialization import row
    subject = Subject(name='列表岗位来源')
    db.add(subject)
    db.flush()
    company = Company(name=subject.name,subject_id=subject.id)
    db.add(company)
    db.flush()
    binding = SourceBinding(company_id=company.id, kind='careers', entry_url='https://careers.example/jobs',
                            config={'website_plan':PLAN.model_dump()})
    task = TaskRun(company_id=company.id, status='running', attempt=1, started_at=now(),
                   scope_hash='inline',config_snapshot={})
    db.add_all([binding,task])
    db.flush()
    run = SourceRun(task_id=task.id,binding_id=binding.id,status='running',scope_hash='inline')
    db.add_all([run,CompanyRun(task_run_id=task.id,company_id=company.id,scope_hash='inline')])
    db.commit()
    filters = {'max_pages':10,'max_details':1}
    urls = []
    def fetch(self,url):
        urls.append(url)
        return url,HTML
    monkeypatch.setattr(PublicFetcher,'fetch',fetch)
    store = OfficialCheckpointStore.open(task.id,row(binding),filters,run.id,1)
    first = collect_with_plan(row(binding),filters,PLAN,checkpoint=store)
    assert first.coverage == 'partial' and len(first.records) == 1
    second = collect_with_plan(row(binding),filters,PLAN,checkpoint=store)
    assert second.coverage == 'complete'
    assert [d.title for d in second.records] == ['算法工程师','运维工程师']
    assert urls == ['https://careers.example/jobs']


def test_verified_button_pagination_advances_query_and_respects_disabled_end():
    from app.website_agent import next_page_url
    plan = PLAN.model_copy(update={'page_query_parameter':'current','next_selector':'.next:not(.disabled)'})
    soup = BeautifulSoup('<button class="next">下一页</button>','html.parser')
    assert next_page_url(soup,plan,'https://careers.example/jobs?current=2&keyword=robot') == 'https://careers.example/jobs?keyword=robot&current=3'
    assert next_page_url(BeautifulSoup('<button class="next disabled"></button>','html.parser'),plan,'https://careers.example/jobs') is None
    with pytest.raises(CollectionError):
        next_page_url(soup,plan,'https://careers.example/jobs?current=bad')


def test_official_metadata_separator_does_not_pollute_city_with_department():
    plan = PLAN.model_copy(update={'location_separator':'|'})
    html = HTML.replace('<p class="city">上海</p>','<p class="city">上海 | 技术类 | 研发部</p>')
    assert inline_jobs(BeautifulSoup(html,'html.parser'),plan,'https://careers.example/jobs')[0].locations == ['上海']
