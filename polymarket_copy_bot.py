from fastapi import FastAPI
app = FastAPI()
@app.get("/status")
def status(): return {"ok": True}
