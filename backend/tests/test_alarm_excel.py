from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.api.v1.alarms import router
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.main import app as production_app
from app.models import AlarmEvent, HcsAccount


def test_alarm_export_is_full_xlsx_and_scoped(mem_db):
    mem_db.add_all([HcsAccount(id=i, code='tenant-'+str(i), name='租户'+str(i), domain_name='',
                              iam_endpoint='https://iam.example.invalid') for i in (1, 2)])
    mem_db.flush()
    mem_db.add_all([AlarmEvent(tenant_id=1, fingerprint='fp-'+str(i), rule_id='rule', service='ecs',
                              resource_type='ecs_server', resource_name='server-'+str(i), severity='P2',
                              status='FIRING', title='=1+1', check_item='state') for i in range(205)])
    mem_db.add(AlarmEvent(tenant_id=2, fingerprint='foreign', rule_id='rule', service='ecs',
                         resource_type='ecs_server', resource_name='foreign', severity='P2', status='FIRING',
                         title='foreign', check_item='state'))
    mem_db.commit()
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    user = CurrentUser(id=1, username='reader', display_name='reader', permissions=['alarm:view'], tenant_ids=[1])
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        response = client.get('/api/v1/alarms/export', params={'status_group':'all','severity':'P2'})
        assert response.status_code == 200
        assert response.content.startswith(b'PK')
        assert response.headers['content-type']=='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        assert response.headers['content-disposition'].endswith('.xlsx')
        ws = load_workbook(BytesIO(response.content))['告警明细']
        assert ws.max_row==206
        assert all(row[1].value=='租户1' and row[8].value!='foreign' for row in list(ws)[1:])
        assert ws['E2'].value=='=1+1' and ws['E2'].data_type=='s'
        filtered = client.get('/api/v1/alarms/export', params={'keyword':'server-204'})
        filtered_ws=load_workbook(BytesIO(filtered.content))['告警明细']
        assert filtered_ws.max_row==2 and filtered_ws['I2'].value=='server-204'
        user.permissions=[]
        assert client.get('/api/v1/alarms/export').status_code==403
