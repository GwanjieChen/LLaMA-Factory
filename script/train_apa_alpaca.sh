export WANDB_PROJECT=starling_apa
deepspeed --num_gpus 8 --master_port=9901 src/train_bash.py \
    --deepspeed ./script/static/zero_stage2.json \
    --stage apa \
    --do_train \
    --model_name_or_path openchat/openchat_3.5 \
    --dataset alpaca_gpt4_en  \
    --split train \
    --template openchat \
    --finetuning_type full \
    --reward_model berkeley-nest/Starling-RM-7B-alpha \
    --reward_model_type full \
    --output_dir results/starling-7B-apa-alpaca \
    --overwrite_output_dir \
    --per_device_train_batch_size 8 \
    --gradient_accumulation_steps 2 \
    --lr_scheduler_type cosine \
    --top_k 0 \
    --top_p 1 \
    --max_new_tokens 1024 \
    --no_ignore_pad_token_for_loss \
    --logging_steps 10 \
    --save_steps 500 \
    --learning_rate 5e-7 \
    --num_train_epochs 5.0 \
    --ppo_epochs 2 \
    --plot_loss \
    --bf16 \
    --run_name alpaca--lm:openchat7b--rm:starling-7b-RM--lr:1e-5--bsize:128 \
    --report_to wandb \
    --logging_step 1 \
    --ppo_logger wandb 