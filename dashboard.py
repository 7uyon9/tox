import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, timedelta

DB_NAME = "cosmetic_inventory.db"

def with_connection(func):
    def wrapper(*args, **kwargs):
        conn = sqlite3.connect(DB_NAME)
        try:
            result = func(conn, *args, **kwargs)
        finally:
            conn.close()
        return result
    return wrapper

###엑셀 데이터 동기화용 함수####
def sync_sheet_to_db(conn, df_sheet, table_name):
    # 간단히 replace 로직. 필요 시 병합 등 세부 로직 변경 가능
    df_sheet.to_sql(table_name, conn, if_exists="append", index=False)

def db_to_sheet(conn, table_name, sheet_name, writer):
    df_db = pd.read_sql(f"SELECT * FROM {table_name}", conn)
    df_db.to_excel(writer, sheet_name=sheet_name, index=False)
        

@with_connection
def get_formula_df(conn, product_name):
    return pd.read_sql(
        'SELECT 원료명, "사용량 (g/%)" FROM formula WHERE 제품명 = ?',
        conn, params=(product_name,)
    )

@with_connection
def create_tables_if_not_exist(conn):
    """
    운영 환경에서 사용하기 위한 테이블 생성 함수입니다.
    이미 테이블이 존재하면 건너뛰고, 없으면 새로 생성합니다.
    DROP TABLE 같은 강제 초기화 로직은 제거했습니다.
    """
    cursor = conn.cursor()

    # inventory 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            원료명 TEXT NOT NULL,
            "재고량 (g)" REAL DEFAULT 0,
            유통기한 TIMESTAMP,
            거래처 TEXT,
            "단가 (원/kg)" REAL,
            "MOQ (kg)" REAL,
            "리드타임 (일)" INTEGER
        )
    """)

    # formula 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS formula (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            제품명 TEXT NOT NULL,
            원료명 TEXT NOT NULL,
            "사용량 (g/%)" REAL DEFAULT 0
        )
    """)

    # production_history 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS production_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            제품명 TEXT NOT NULL,
            "용량 (g)" REAL DEFAULT 0,   -- 새로 추가 (1개당 용량)
            수량 INTEGER NOT NULL,       -- 기존 '생산량' 대신 이름 변경
            날짜 TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # transactions 테이블 (비고 컬럼 추가)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            원료명 TEXT NOT NULL,
            유형 TEXT NOT NULL,
            "수량 (g)" REAL DEFAULT 1,
            날짜 TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            비고 TEXT
        )
    """)

    conn.commit()
  

# 함수: 엑셀 데이터를 데이터베이스로 업데이트
@with_connection
def sync_excel_to_db_with_update(conn, excel_file, sheet_name, table_name):
    # 엑셀 파일에서 데이터 읽어오기
    df_excel = pd.read_excel(excel_file, sheet_name=sheet_name)

    # 필요시 불필요한 Unnamed 컬럼 제거
    df_excel = df_excel.loc[:, ~df_excel.columns.str.contains('^Unnamed')]

    # 기존 DB 데이터 가져오기
    try:
        df_db = pd.read_sql(f"SELECT * FROM {table_name}", conn)
    except Exception:
        df_db = pd.DataFrame()

    if not df_db.empty:
        df_merged = pd.concat([df_db, df_excel]).drop_duplicates(subset=df_excel.columns.tolist(), keep='last')
    else:
        df_merged = df_excel

    df_merged.to_sql(table_name, conn, if_exists="append", index=False)
    st.success(f"엑셀 데이터({sheet_name})가 데이터베이스({table_name})로 업데이트 및 병합되었습니다.")

# 함수: 데이터베이스 데이터를 엑셀로 내보내기
@with_connection
def sync_db_to_excel(conn, table_name, sheet_name):
    df = pd.read_sql(f"SELECT * FROM {table_name}", conn)

    with pd.ExcelWriter("exported_data.xlsx", engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)

    st.success("엑셀 파일이 생성되었습니다. 아래 버튼을 눌러 다운로드하세요.")
    st.download_button(
        label="엑셀 파일 다운로드",
        data=open("exported_data.xlsx", "rb").read(),
        file_name="exported_data.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

######################
# Streamlit 메인
######################
st.set_page_config(page_title="Cosmetic Inventory", layout="wide")

# 필요하다면, 초기 1회만 아래 함수 호출하여 테이블 구조 보장
create_tables_if_not_exist()

menu = st.sidebar.selectbox(
    "메뉴를 선택하세요",
    ["홈", "재고 관리", "입출고 기록", "유통기한 관리", "생산 가능량 계산", "생산 히스토리", "엑셀 동기화"]
)

if menu == "홈":
    st.title("홈 대시보드")

    @with_connection
    def display_dashboard_summary(conn):
        try:
            inventory_df = pd.read_sql("SELECT COUNT(*) AS 총원료수 FROM inventory", conn)
            expiring_df = pd.read_sql(
                "SELECT COUNT(*) AS 임박원료수 FROM inventory WHERE 유통기한 IS NOT NULL AND 유통기한 <= ?",
                conn,
                params=(datetime.now() + timedelta(days=30),)
            )
            total_ingredients = inventory_df.iloc[0, 0] if not inventory_df.empty else 0
            expiring_ingredients = expiring_df.iloc[0, 0] if not expiring_df.empty else 0
            st.metric("총 원료 수", total_ingredients)
            st.metric("유통기한 임박 원료 수", expiring_ingredients)
        except Exception as e:
            st.error(f"대시보드를 불러오는 중 오류가 발생했습니다: {e}")

    display_dashboard_summary()

elif menu == "재고 관리":
    st.title("재고 관리")
    
    @with_connection
    def get_inventory(conn):
        # id를 포함한 전체 데이터를 불러옴
        return pd.read_sql("SELECT * FROM inventory ORDER BY 원료명 ASC", conn)
    
    # 재고 데이터 읽어오기
    inventory_df = get_inventory()
    
    st.write("아래 표를 직접 수정하세요. (ID 열은 읽기 전용)")
    # st.data_editor (또는 st.experimental_data_editor)를 사용하여 편집 가능하도록 함
    edited_df = st.data_editor(
        inventory_df,
        num_rows="dynamic",    # 필요시 행 추가도 허용
        disabled=["id","유통기한"]        # id 컬럼은 수정할 수 없도록 설정
    )
    
    if st.button("수정 내용 저장"):
        # 수정된 DataFrame의 각 행에 대해 데이터베이스 업데이트 실행
        @with_connection
        def update_inventory_from_df(conn, df):
            cursor = conn.cursor()
            # 각 행에 대해 UPDATE 쿼리 실행 (id를 기준으로)
            for _, row in df.iterrows():
                update_sql = """
                    UPDATE inventory
                    SET 원료명 = ?,
                        "재고량 (g)" = ?,
                        유통기한 = ?,
                        거래처 = ?,
                        "단가 (원/kg)" = ?,
                        "MOQ (kg)" = ?,
                        "리드타임 (일)" = ?
                    WHERE id = ?
                """
                # 유통기한은 문자열 형식(예: 'YYYY-MM-DD')로 저장한다고 가정
                cursor.execute(update_sql, (
                    row["원료명"],
                    row["재고량 (g)"],
                    str(row["유통기한"]) if pd.notna(row["유통기한"]) else None,
                    row["거래처"],
                    row["단가 (원/kg)"],
                    row["MOQ (kg)"],
                    row["리드타임 (일)"],
                    row["id"]
                ))
            conn.commit()
        
        update_inventory_from_df(edited_df)
        st.success("수정된 재고 데이터가 저장되었습니다.")


## 유통기한 수정
    @with_connection
    def get_all_inventory(conn):
        return pd.read_sql("SELECT * FROM inventory", conn)

    @with_connection
    def update_expiration_date(conn, row_id, new_date):
        cursor = conn.cursor()
        cursor.execute("UPDATE inventory SET 유통기한 = ? WHERE id = ?", (new_date, row_id))
        conn.commit()

    st.subheader("유통기한 수정")
    all_inv = get_all_inventory()
    if all_inv.empty:
        st.info("등록된 원료가 없습니다.")
    else:
        # 표시명 생성: [ID 12] 살구씨오일 (현재: 2025-01-01)
        all_inv["표시명"] = all_inv.apply(
            lambda x: f"[ID {x['id']}] {x['원료명']} (현재: {x['유통기한']})",
            axis=1
        )
        selected_row = st.selectbox(
            "유통기한을 수정할 원료 선택",
            all_inv["표시명"].tolist()
        )

        import re
        selected_id = None
        if selected_row:
            # [ID 12] 형태에서 12를 추출
            m = re.search(r"\[ID (\d+)\]", selected_row)
            if m:
                selected_id = int(m.group(1))

        new_date = st.date_input("새 유통기한 설정", value=None)
        if st.button("유통기한 수정"):
            if selected_id:
                update_expiration_date(selected_id, str(new_date))
                st.success("유통기한이 성공적으로 업데이트되었습니다.")
            else:
                st.warning("원료를 제대로 선택해주세요.")

elif menu == "입출고 기록":
    st.title("입출고 기록")

    ############################################################
    # 유틸 함수: inventory에 새 원료 추가
    ############################################################

    @with_connection
    def get_vendor_list(conn):
        df = pd.read_sql("SELECT DISTINCT 거래처 FROM inventory WHERE 거래처 IS NOT NULL AND 거래처 != ''", conn)
        vendors = df["거래처"].dropna().unique().tolist()
        return vendors


    @with_connection
    def add_new_ingredient(conn, 원료명, 유통기한, 거래처, 단가, MOQ, 리드타임):
        cursor = conn.cursor()
        insert_sql = """
            INSERT INTO inventory
            (원료명, "재고량 (g)", 유통기한, 거래처, "단가 (원/kg)", "MOQ (kg)", "리드타임 (일)")
            VALUES (?, 0, ?, ?, ?, ?, ?)
        """
        cursor.execute(insert_sql, (원료명, 유통기한, 거래처, 단가, MOQ, 리드타임))
        conn.commit()
    
    @with_connection
    def get_inventory_list(conn):
        df_inv = pd.read_sql("SELECT 원료명 FROM inventory ORDER BY 원료명 ASC", conn)
        return df_inv["원료명"].unique().tolist() if not df_inv.empty else []
    
    @with_connection
    def record_transaction(conn, 원료명, 유형, 수량_g, memo):
        try:
            cursor = conn.cursor()
            # 비고 컬럼까지 INSERT
            transaction_query = """
                INSERT INTO transactions
                (원료명, 유형, "수량 (g)", 날짜, 비고)
                VALUES (?, ?, ?, ?, ?)
            """
            cursor.execute(transaction_query, (
                원료명, 유형, float(num_or_zero(str(수량_g))),
                datetime.now(), memo
            ))
    
            # 재고 테이블 업데이트
            if 유형 == "입고":
                update_query = """
                    UPDATE inventory
                    SET "재고량 (g)" = "재고량 (g)" + ?
                    WHERE 원료명 = ?
                """
            else:  # 출고
                update_query = """
                    UPDATE inventory
                    SET "재고량 (g)" = "재고량 (g)" - ?
                    WHERE 원료명 = ?
                """
            cursor.execute(update_query, (수량_g, 원료명))
    
            conn.commit()
            st.success(f"{유형} 기록 완료! (원료: {원료명}, 수량(g): {수량_g}, 비고: {memo})")
    
        except Exception as e:
            st.error(f"거래 기록 중 오류가 발생했습니다: {e}")
    
    def num_or_zero(text):
        try:
            return float(text)
        except:
            return 0.0
    
    @with_connection
    def display_transactions(conn):
        try:
            df = pd.read_sql('''
                SELECT
                    id,
                    날짜,
                    원료명,
                    유형,
                    "수량 (g)" as "수량",
                    비고
                FROM transactions
                ORDER BY id DESC
            ''', conn)
            # "날짜" 컬럼을 datetime으로 변환한 후, "YYYY-MM-DD" 형식으로 변경
            if "날짜" in df.columns:
                df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.strftime("%Y-%m-%d")

            st.dataframe(df)
        except Exception as e:
            st.error(f"입출고 기록을 불러오는 중 오류가 발생했습니다: {e}")
    
    @with_connection
    def delete_transaction_and_restore_inventory(conn, trans_id):
        cursor = conn.cursor()
        # 해당 거래 정보 가져오기
        select_q = 'SELECT 원료명, 유형, "수량 (g)" FROM transactions WHERE id = ?'
        cursor.execute(select_q, (trans_id,))
        row = cursor.fetchone()
        if row:
            ingr, ttype, amt = row
            # 재고 복원
            if ttype == "입고":
                # 입고를 취소하므로 재고에서 빼기
                revert_q = """
                    UPDATE inventory
                    SET "재고량 (g)" = "재고량 (g)" - ?
                    WHERE 원료명 = ?
                """
            else:  # 출고 취소하므로 재고에 다시 더하기
                revert_q = """
                    UPDATE inventory
                    SET "재고량 (g)" = "재고량 (g)" + ?
                    WHERE 원료명 = ?
                """
            cursor.execute(revert_q, (amt, ingr))
    
            # 해당 거래 삭제
            del_q = "DELETE FROM transactions WHERE id = ?"
            cursor.execute(del_q, (trans_id,))
    
            conn.commit()
            st.success(f"거래 ID {trans_id} 삭제, 재고 복원 완료!")
        else:
            st.error("해당 거래 ID를 찾을 수 없습니다.")
    
      

    # 기존/신규 원료 선택 라디오
    ingredient_mode = st.radio("원료 선택 방법", ("기존 원료", "새 원료"))

    if ingredient_mode == "기존 원료":
        existing_ingredients = get_inventory_list()
        if existing_ingredients:
            selected_ingr = st.selectbox("원료명을 선택하세요", existing_ingredients)
            transaction_type = st.selectbox("유형을 선택하세요", ["입고", "출고"])
            amount = st.number_input("수량 (g)", min_value=0.0, step=0.1)
            memo_exist = st.text_input("비고(메모)", value="", key="memo_exist")
            if st.button("거래 기록 저장", key="existing_ingr"):
                record_transaction(selected_ingr, transaction_type, amount, memo_exist)
        else:
            st.info("현재 등록된 원료가 없습니다. 새 원료를 추가해주세요.")

    else:  # "새 원료"
        st.subheader("새 원료 추가 후 거래 기록")
        new_ingr_name = st.text_input("원료명")
        new_ingr_date = st.date_input("유통기한", value=None)
        
        # 거래처 입력: 기존 거래처 선택 또는 직접 입력
        vendors = get_vendor_list()
        vendors_options = vendors + ["직접 입력"]
        selected_vendor = st.selectbox("거래처 선택", vendors_options)
        if selected_vendor == "직접 입력":
            new_vendor = st.text_input("새 거래처 입력")
            final_vendor = new_vendor.strip()
        else:
            final_vendor = selected_vendor

        new_ingr_price = st.number_input("단가 (원/kg)", min_value=0.0, step=0.1)
        new_ingr_moq = st.number_input("MOQ (kg)", min_value=0.0, step=0.1)
        new_ingr_lead = st.number_input("리드타임 (일)", min_value=0, step=1)
        ttype2 = st.selectbox("유형을 선택하세요", ["입고", "출고"])
        amt2 = st.number_input("수량 (g)", min_value=0.0, step=0.1, key="amt2")
        memo_new = st.text_input("비고(메모)", value="", key="memo_new")

        if st.button("신규 원료 생성 및 거래 기록", key="new_ingr"):
            if not new_ingr_name.strip():
                st.warning("원료명을 입력하세요.")
            else:
                add_new_ingredient(
                    원료명=new_ingr_name.strip(),
                    유통기한=str(new_ingr_date) if new_ingr_date else None,
                    거래처=final_vendor,  # 최종 선택된 거래처 사용
                    단가=new_ingr_price,
                    MOQ=new_ingr_moq,
                    리드타임=new_ingr_lead
                )
                record_transaction(new_ingr_name.strip(), ttype2, amt2, memo_new)


    st.subheader("입출고 이력")
    display_transactions()

    st.subheader("거래 삭제")
    del_id = st.number_input("삭제할 거래 ID", min_value=1, step=1)
    if st.button("거래 삭제"):
        delete_transaction_and_restore_inventory(del_id)


elif menu == "유통기한 관리":
    st.title("유통기한 관리")


    @with_connection
    def display_expiring_items(conn, days):
        query = "SELECT * FROM inventory WHERE 유통기한 IS NOT NULL"
        df = pd.read_sql(query, conn)
        df["유통기한"] = pd.to_datetime(df["유통기한"], errors="coerce")
        warning_date = datetime.now() + timedelta(days=days)
        return df[df["유통기한"] <= warning_date]

    st.subheader("임박 원료 조회")
    days = st.slider("임박 기준 일수", min_value=1, max_value=365, value=30)
    expiring_df = display_expiring_items(days)
    # "유통기한" 컬럼이 datetime 타입이면, YYYY-MM-DD 형식의 문자열로 변환
    if not expiring_df.empty and "유통기한" in expiring_df.columns:
        expiring_df["유통기한"] = pd.to_datetime(expiring_df["유통기한"], errors="coerce").dt.strftime("%Y-%m-%d")
    st.dataframe(expiring_df)




elif menu == "생산 가능량 계산":
    st.title("생산 가능량 계산")

    # 세션 상태 초기화
    if "calc_done" not in st.session_state:
        st.session_state.calc_done = False
        st.session_state.used_table = []
        st.session_state.sufficient = False

    @with_connection
    def get_product_list(conn):
        return pd.read_sql("SELECT DISTINCT 제품명 FROM formula", conn)["제품명"].tolist()


    @with_connection
    def get_inventory_df(conn):
        return pd.read_sql("SELECT * FROM inventory", conn)

    @with_connection
    def insert_production_history(conn, 제품명, 용량_g, 수량, used_materials):
        cursor = conn.cursor()
        # production_history에 기록
        insert_sql = """
            INSERT INTO production_history (제품명, "용량 (g)", 수량, 날짜)
            VALUES (?, ?, ?, ?)
        """
        cursor.execute(insert_sql, (제품명, 용량_g, 수량, datetime.now()))
        new_id = cursor.lastrowid

        # 재고 차감
        for (ingredient, used_qty) in used_materials:
            upd_sql = """
                UPDATE inventory
                SET "재고량 (g)" = "재고량 (g)" - ?
                WHERE 원료명 = ?
            """
            cursor.execute(upd_sql, (used_qty, ingredient))

        conn.commit()
        return new_id

    product_list = get_product_list()
    product_name = st.selectbox("제품명을 선택하세요:", product_list)

    if product_name:
        formula_df = get_formula_df(product_name)
        if formula_df.empty:
            st.warning(f"{product_name}에 대한 처방이 없습니다.")
        else:
            st.write(f"'{product_name}'의 처방:")
            st.dataframe(formula_df)

            unit_capacity = st.number_input("1개당 용량 (g)", min_value=1)
            total_quantity = st.number_input("생산할 총 수량 (개)", min_value=1)

            # (1) 생산 가능량 계산
            if st.button("생산 가능량 계산"):
                inv_df = get_inventory_df()
                used_table = []  # [(원료명, 필요량, 보유량)]
                sufficient = True

                for _, row in formula_df.iterrows():
                    ingredient = row["원료명"]
                    usage_per_unit = row["사용량 (g/%)"]
                    required_qty = usage_per_unit * unit_capacity * total_quantity

                    current_stock = inv_df.loc[inv_df["원료명"] == ingredient, "재고량 (g)"].sum()
                    used_table.append((ingredient, required_qty, current_stock))

                    if current_stock < required_qty:
                        st.error(f"{ingredient} 재고 부족 -> 필요 {required_qty}g, 보유 {current_stock}g")
                        sufficient = False

                # 세션 상태에 저장
                st.session_state.calc_done = True
                st.session_state.used_table = used_table
                st.session_state.sufficient = sufficient

            # 계산 후 표시
            if st.session_state.calc_done:
                # 화면에 사용량 테이블 표시
                usage_df = pd.DataFrame(
                    [(x[0], x[1], x[2]) for x in st.session_state.used_table],
                    columns=["원료명", "필요량(g)", "현재재고(g)"]
                )
                st.write("원료별 사용량 계산 결과:")
                st.dataframe(usage_df)

                # (2) 충분하면 생산 진행 확인
                if st.session_state.sufficient:
                    if st.button("생산 진행 확인"):
                        used_materials = [(x[0], x[1]) for x in st.session_state.used_table]
                        new_id = insert_production_history(product_name, unit_capacity, total_quantity, used_materials)
                        st.success(f"{product_name} {total_quantity}개 생산 완료! (ID={new_id})")
                        # 재설정 (버튼 누른 뒤 UI를 초기화)
                        st.session_state.calc_done = False
                        st.session_state.used_table = []
                        st.session_state.sufficient = False



elif menu == "엑셀 동기화":
    """
    자동으로 정해진 시트 <-> 테이블 매핑을 수행하고
    사용자는 업로드(Excel->DB) 또는 다운로드(DB->Excel)만 간단히 선택 가능.
    """
    st.title("엑셀 데이터 동기화")


    # (1) Excel -> DB 업로드
    uploaded_file = st.file_uploader("엑셀 파일을 업로드하세요", type=["xlsx"])
    if uploaded_file:
        if st.button("업로드 실행 (엑셀 -> DB)"):
            try:
                import sqlite3
                # 미리 정해진 시트명에 따라 엑셀 파일의 데이터를 읽어옵니다.
                df_inv = pd.read_excel(uploaded_file, sheet_name="재고", engine="openpyxl")
                df_for = pd.read_excel(uploaded_file, sheet_name="처방", engine="openpyxl")
                df_trans = pd.read_excel(uploaded_file, sheet_name="입출고", engine="openpyxl")
                df_prod = pd.read_excel(uploaded_file, sheet_name="생산이력", engine="openpyxl")

                # 하나의 DB 연결을 열어서 모든 시트의 데이터를 업데이트합니다.
                conn = sqlite3.connect(DB_NAME)
                try:
                    sync_sheet_to_db(conn, df_inv, "inventory")
                    sync_sheet_to_db(conn, df_for, "formula")
                    sync_sheet_to_db(conn, df_trans, "transactions")
                    sync_sheet_to_db(conn, df_prod, "production_history")
                finally:
                    conn.commit()
                    conn.close()

                st.success("엑셀 업로드 및 DB 반영이 완료되었습니다.")
            except Exception as e:
                st.error(f"업로드 중 오류 발생: {e}")

    # (2) DB -> Excel 내보내기
    if st.button("내보내기 실행 (DB -> Excel)"):
        try:
            import openpyxl
            with pd.ExcelWriter("exported_data.xlsx", engine="openpyxl") as writer:
                conn = sqlite3.connect(DB_NAME)
                try:
                    db_to_sheet(conn, "inventory", "재고", writer)
                    db_to_sheet(conn, "formula", "처방", writer)
                    db_to_sheet(conn, "transactions", "입출고", writer)
                    db_to_sheet(conn, "production_history", "생산이력", writer)
                finally:
                    conn.close()
                
                # 모든 시트의 상태를 visible로 설정
                wb = writer.book
                for sheet in wb.worksheets:
                    sheet.sheet_state = 'visible'
                
                if "Sheet" in wb.sheetnames and len(wb.sheetnames) > 1:
                    wb.remove(wb["Sheet"])
            
            # 파일명: [YY-MM-DD 원료 재고 및 생산일지.xlsx]
            filename = f"{datetime.now().strftime('%y-%m-%d')} 원료 재고 및 생산일지.xlsx"
            
            st.success("엑셀 파일이 생성되었습니다. 아래 버튼을 눌러 다운로드하세요.")
            st.download_button(
                "엑셀 다운로드", 
                data=open("exported_data.xlsx", "rb").read(),
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        except Exception as e:
            st.error(f"내보내기 중 오류 발생: {e}")






elif menu == "생산 히스토리":
    st.title("생산 히스토리")

    @with_connection
    def get_production_history_all(conn):
        # '용량 (g)'과 '수량' 컬럼을 함께 읽어옴
        df = pd.read_sql(
            """SELECT id, 제품명, "용량 (g)" as 용량, 수량, 날짜
            FROM production_history
            ORDER BY id DESC""",
            conn
        )
        # "날짜" 컬럼을 "YYYY-MM-DD" 형식으로 변환
        if "날짜" in df.columns:
            df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.strftime("%Y-%m-%d")
        return df

    # formula 테이블에서 처방 읽기 (이미 있음)
    # get_formula_df(product_name)

    # UI: 전체 히스토리 표시
    history_df = get_production_history_all()
    st.dataframe(history_df)

    # 특정 기록 상세 보기
    selected_hist_id = st.number_input("상세보기할 생산 이력 ID", min_value=1, step=1)
    if st.button("생산 이력 상세 조회"):
        row_data = history_df.loc[history_df["id"] == selected_hist_id]
        if row_data.empty:
            st.error("해당 ID가 없습니다.")
        else:
            prod_name = row_data.iloc[0]["제품명"]
            unit_capacity = row_data.iloc[0]["용량"]
            total_qty = row_data.iloc[0]["수량"]

            st.write(f"제품명: {prod_name}, 1개당 용량(g): {unit_capacity}, 수량(개): {total_qty}")
            # 처방 불러오기
            f_df = get_formula_df(prod_name)
            if f_df.empty:
                st.warning("처방이 없습니다.")
            else:
                # 원료별 사용량 = formula["사용량 (g/%)"] * unit_capacity * total_qty
                detail_rows = []
                for _, rowf in f_df.iterrows():
                    ingr = rowf["원료명"]
                    usage_per_unit = rowf["사용량 (g/%)"]
                    needed = usage_per_unit * unit_capacity * total_qty
                    detail_rows.append((ingr, usage_per_unit, needed))

                detail_df = pd.DataFrame(detail_rows, columns=["원료명", "처방 (g/%)", "실제 사용량(g)"])
                st.write("이 생산에 사용된 구체적 원료 내역:")
                st.dataframe(detail_df)

    @with_connection
    def delete_production_history_and_restore(conn, hist_id):
        """
        1) production_history에서 (제품명, 용량, 수량) 확인
        2) formula 테이블로 각 원료별 사용량(g/%) 불러옴
        3) 실제 사용량 = (단위용량 * 수량 * 처방)
        4) inventory 재고량을 복원
        5) production_history 행 삭제
        """
        cursor = conn.cursor()

        # 1) 해당 히스토리 조회
        select_q = """SELECT 제품명, "용량 (g)" as 용량, 수량
                    FROM production_history
                    WHERE id = ?"""
        cursor.execute(select_q, (hist_id,))
        row = cursor.fetchone()
        if not row:
            return False, "해당 생산 이력을 찾을 수 없습니다."
        product_name, unit_capacity, total_qty = row

        # 2) formula(처방)에서 원료별 사용량 불러오기
        df_formula = get_formula_df(product_name)
        if df_formula.empty:
            return False, f"'{product_name}'에 대한 처방이 없어 복원이 불가능합니다."

        # 3) 실제 사용량 계산
        for _, r in df_formula.iterrows():
            ingr = r["원료명"]
            usage_per_unit = r["사용량 (g/%)"]
            used_amount = usage_per_unit * unit_capacity * total_qty

            # 4) inventory 복원
            upd_q = """UPDATE inventory
                    SET "재고량 (g)" = "재고량 (g)" + ?
                    WHERE 원료명 = ?"""
            cursor.execute(upd_q, (used_amount, ingr))

        # 5) 생산 히스토리 삭제
        del_q = "DELETE FROM production_history WHERE id = ?"
        cursor.execute(del_q, (hist_id,))
        conn.commit()

        return True, f"ID={hist_id} 생산 이력을 삭제하고 재고를 복원했습니다."


    # 생산 히스토리 삭제 UI
    delete_hist_id = st.number_input("삭제할 생산 이력 ID", min_value=1, step=1)
    if st.button("생산 이력 삭제"):
        ok, msg = delete_production_history_and_restore(delete_hist_id)
        if ok:
            st.success(msg)
        else:
            st.error(msg)
