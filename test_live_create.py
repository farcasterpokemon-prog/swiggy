import swiggy_signup as ss
import swiggy_api_signup as api
import time, json, uuid

cfg = ss.load_config('swiggy_signup.json')
prov = ss.make_provider(cfg['otp_provider'])
print('Provider:', type(prov).__name__, 'Balance:', prov.get_balance())

# Loop to find a fresh number
phone, order_id = None, None
for attempt in range(1, 20):
    print(f'Attempt {attempt}: Renting number...')
    p, o = prov.get_number()
    print(f'Rented: {p} (Order #{o})')
    
    # Check registration
    reg, check_data = ss.check_swiggy_registered(p, cfg)
    st = check_data.get('status')
    print(f'Pre-check {p}: is_registered={reg}, status={st}')
    
    if reg or st == 'registered':
        print(f'Rejecting registered number {p}, cancelling order {o}...')
        prov.set_status(o, -1)
        time.sleep(1)
        continue
    
    print(f'FOUND UNREGISTERED/FRESH NUMBER: {p}!')
    phone, order_id = p, o
    break

if not phone:
    print('No fresh number found in 20 attempts.')
    exit(1)

# 3. Request OTP from Swiggy
swuid = uuid.uuid4().hex[:16]
print(f'Sending Swiggy OTP for {phone} (swuid={swuid})...')
code, otp_resp = api.send_otp(phone, swuid)
print(f'send_otp HTTP {code} => {json.dumps(otp_resp, indent=2)}')

if code != 200 or otp_resp.get('statusCode') != 0:
    print('Failed to request OTP! Cancelling order...')
    prov.set_status(order_id, -1)
    exit(1)

tid0 = otp_resp.get('tid', '')
sid0 = otp_resp.get('sid', '')

# 4. Wait for OTP from provider
print('Waiting for OTP from provider (up to 75s)...')
otp = None
end = time.time() + 75
while time.time() < end:
    sms = prov.fetch_otp(order_id)
    print(f'fetch_otp raw: {sms}')
    if sms:
        otp = ss.extract_otp(sms)
        if otp:
            print(f'EXTRACTED OTP: {otp}')
            break
    time.sleep(2.5)

if not otp:
    print('No OTP received. Cancelling order...')
    prov.set_status(order_id, -1)
    exit(1)

# 5. Verify OTP on Swiggy
print(f'Verifying OTP {otp} with Swiggy...')
v_code, v_data = api.verify_otp(otp, tid0, sid0, swuid)
print(f'verify_otp HTTP {v_code} => {json.dumps(v_data, indent=2)}')

# 6. If fresh, signup
sess_data = v_data.get('data') or {}
is_reg = bool(sess_data.get('registered', False))
print(f'is_registered: {is_reg}')

final_data = v_data
if not is_reg:
    print('Calling signup...')
    tid1 = v_data.get('tid') or (v_data.get('data') or {}).get('tid') or tid0
    sid1 = v_data.get('sid') or (v_data.get('data') or {}).get('sid') or sid0
    s_code, s_data = api.signup(phone, 'Swiggy Test User', tid1, sid1, swuid)
    print(f'signup HTTP {s_code} => {json.dumps(s_data, indent=2)}')
    if s_code == 200 and s_data.get('statusCode') in [0, '0']:
        final_data = s_data

# 7. Extract account
acct = api.extract_account_dict(final_data, phone, tid0, sid0, swuid)
print('Extracted account dict:', json.dumps(acct, indent=2))

# 8. Live verify
ok, live_acct = api.verify_session_live(acct)
print(f'Live verify result: {ok}, account: {json.dumps(live_acct, indent=2)}')
