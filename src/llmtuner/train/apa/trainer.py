import math
import os
import sys
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import torch
from tqdm import tqdm
from transformers import GenerationConfig, Trainer, TrainerControl, TrainerState
from transformers.trainer_pt_utils import remove_dummy_checkpoint
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR
from transformers.utils import SAFE_WEIGHTS_NAME, WEIGHTS_NAME
from trl.core import PPODecorators, logprobs_from_logits
from .apa_trainer import APATrainer
from ...extras.callbacks import FixValueHeadModelCallback, LogCallback
from ...extras.logging import get_logger
from ...extras.misc import AverageMeter, count_parameters, get_logits_processor
from .utils import dump_layernorm, get_rewards_from_server, replace_model, restore_layernorm


if TYPE_CHECKING:
    from transformers import Seq2SeqTrainingArguments, TrainerCallback
    from trl import AutoModelForCausalLMWithValueHead

    from ...hparams import FinetuningArguments, GeneratingArguments, ModelArguments


logger = get_logger(__name__)


class CustomAPATrainer(APATrainer, Trainer):
    r"""
    Inherits APATrainer.
    """

    def __init__(
        self,
        model_args: "ModelArguments",
        training_args: "Seq2SeqTrainingArguments",
        finetuning_args: "FinetuningArguments",
        generating_args: "GeneratingArguments",
        callbacks: List["TrainerCallback"],
        reward_model: "AutoModelForCausalLMWithValueHead",
        **kwargs,
    ):
        APATrainer.__init__(self, **kwargs)

        self.args = training_args
        self.model_args = model_args
        self.finetuning_args = finetuning_args
        self.reward_model = reward_model

        self.generation_config = GenerationConfig(
            pad_token_id=self.tokenizer.pad_token_id,
            eos_token_id=[self.tokenizer.eos_token_id] + self.tokenizer.additional_special_tokens_ids,
            **generating_args.to_dict(),
        )

        self.state = TrainerState()
        self.control = TrainerControl()
        self.is_deepspeed_enabled = self.accelerator.distributed_type == "DEEPSPEED" and hasattr(
            self.accelerator.state, "deepspeed_plugin"
        )
        self.log_callback, self.save_callback = callbacks[0], callbacks[1]
        assert isinstance(self.log_callback, LogCallback) and isinstance(self.save_callback, FixValueHeadModelCallback)

        if self.args.max_steps > 0:
            logger.info("max_steps is given, it will override any value given in num_train_epochs")

        if finetuning_args.reward_model_type == "full":
            if self.is_deepspeed_enabled:
                # if not (
                #     getattr(reward_model.pretrained_model, "is_loaded_in_8bit", False)
                #     or getattr(reward_model.pretrained_model, "is_loaded_in_4bit", False)
                # ):  # quantized models are already set on the correct device
                #     
                self.reward_model = self._prepare_deepspeed(self.reward_model)
            else:
                self.reward_model = self.accelerator.prepare_model(self.reward_model, evaluation_mode=True)

    def apa_train(self, resume_from_checkpoint: Optional[str] = None) -> None:
        r"""
        Implements training loop for the APA stage, like _inner_training_loop() in Huggingface's Trainer.
        """
        if resume_from_checkpoint is not None:
            raise ValueError("`resume_from_checkpoint` will be supported in the future version.")

        total_train_batch_size = (
            self.args.per_device_train_batch_size
            * self.args.gradient_accumulation_steps
            * self.finetuning_args.ppo_buffer_size
            * self.args.world_size
        )
        if self.args.max_steps > 0:
            num_examples = total_train_batch_size * self.args.max_steps
            num_train_epochs = sys.maxsize
            max_steps = self.args.max_steps
            steps_in_epoch = self.args.max_steps
        else:
            len_dataloader = len(self.dataloader)
            num_examples = len(self.dataset)
            num_train_epochs = self.args.num_train_epochs
            max_steps = math.ceil(num_train_epochs * len_dataloader)
            steps_in_epoch = len_dataloader

        self.state.max_steps = max_steps
        self.state.num_train_epochs = num_train_epochs
        self.state.is_local_process_zero = self.is_local_process_zero()
        self.state.is_world_process_zero = self.is_world_process_zero()

        if self.is_world_process_zero():
            logger.info("***** Running training *****")
            logger.info("  Num examples = {}".format(num_examples))
            logger.info("  Num Epochs = {}".format(num_train_epochs))
            logger.info("  Instantaneous batch size per device = {}".format(self.args.per_device_train_batch_size))
            logger.info(
                "  Total train batch size (w. parallel, buffer, distributed & accumulation) = {}".format(
                    total_train_batch_size
                )
            )
            logger.info("  Gradient Accumulation steps = {}".format(self.args.gradient_accumulation_steps))
            logger.info("  Num optimization epochs per batch = {}".format(self.finetuning_args.ppo_epochs))
            logger.info("  Total training steps = {}".format(max_steps))
            logger.info("  Number of trainable parameters = {}".format(count_parameters(self.model)[0]))

        unwrapped_model: "AutoModelForCausalLMWithValueHead" = self.accelerator.unwrap_model(self.model)
        dataiter = iter(self.dataloader)
        loss_meter = AverageMeter()
        reward_meter = AverageMeter()
        self.log_callback.on_train_begin(self.args, self.state, self.control)

        for step in tqdm(range(max_steps), disable=not self.is_local_process_zero()):
            try:
                batch = next(dataiter)
            except StopIteration:
                dataiter = iter(self.dataloader)
                batch = next(dataiter)

            # Cast to inference mode
            unwrapped_model.gradient_checkpointing_disable()
            unwrapped_model.config.use_cache = True
            self.model.eval()

            # Get inputs
            self.tokenizer.padding_side = "right"  # change padding side
            queries, responses, rewards = [], [], []
            for idx in range(0, self.config.batch_size, self.config.mini_batch_size):
                mini_batch_queries, mini_batch_responses = self.get_inputs(
                    batch[idx : idx + self.config.mini_batch_size]
                )
                # mini_batch_rewards = self.get_rewards(mini_batch_queries, mini_batch_responses, unwrapped_model)
                mini_batch_rewards = self.get_rewards_Starling(mini_batch_queries, mini_batch_responses, unwrapped_model, self.finetuning_args.sigmoid_score)
                queries.extend(mini_batch_queries)
                responses.extend(mini_batch_responses)
                rewards.extend(mini_batch_rewards)

            # Cast to training mode
            unwrapped_model.gradient_checkpointing_enable()
            unwrapped_model.config.use_cache = False
            self.model.train()

            # Run APA step
            stats = self.step(queries, responses, rewards)
            self.tokenizer.padding_side = "left"  # restore padding side
            loss_meter.update(float(stats["apa/loss/total"]), n=len(rewards))
            reward_meter.update(torch.stack(rewards).mean().item(), n=len(rewards))

            if self.config.log_with is not None:
                try:
                    batch["query"] = self.tokenizer.batch_decode(queries, skip_special_tokens=True)
                    batch["response"] = self.tokenizer.batch_decode(responses, skip_special_tokens=True)
                    self.log_stats(stats, batch, rewards)
                except Exception:
                    logger.warning("Failed to save stats due to unknown errors.")

            self.state.global_step += 1
            self.log_callback.on_step_end(self.args, self.state, self.control)

            if self.is_local_process_zero() and (step + 1) % self.args.logging_steps == 0:
                logs = dict(
                    loss=round(loss_meter.avg, 4),
                    reward=round(reward_meter.avg, 4),
                    learning_rate=stats["apa/learning_rate"],
                    epoch=round(step / steps_in_epoch, 2),
                )
                tqdm.write(str(logs))
                logs["step"] = step
                self.state.log_history.append(logs)
                self.log_callback.on_log(self.args, self.state, self.control)
                loss_meter.reset()
                reward_meter.reset()

            if (step + 1) % self.args.save_steps == 0:  # save checkpoint
                self.save_model(
                    os.path.join(self.args.output_dir, "{}-{}".format(PREFIX_CHECKPOINT_DIR, self.state.global_step))
                )
                self.save_callback.on_save(
                    self.args, self.state, self.control, model=self.accelerator.unwrap_model(self.model), generate_config=self.generation_config
                )

            if self.control.should_epoch_stop or self.control.should_training_stop:
                break

        self.log_callback.on_train_end(self.args, self.state, self.control)
        self.save_callback.on_train_end(
            self.args, self.state, self.control, model=self.accelerator.unwrap_model(self.model)
        )

    @torch.no_grad()
    def get_inputs(self, batch: Dict[str, torch.Tensor]) -> Tuple[List[torch.Tensor], List[torch.Tensor]]:
        r"""
        Generates model's responses given queries.
        """
        if self.model_args.upcast_layernorm:
            layernorm_params = dump_layernorm(self.model)

        if batch["input_ids"].size(0) == 1:  # handle llama2 apa with gradient accumulation > 1
            start_index = (batch["input_ids"][0] != self.tokenizer.pad_token_id).nonzero()[0].item()
            for k, v in batch.items():
                batch[k] = v[:, start_index:]

        unwrapped_model: "AutoModelForCausalLMWithValueHead" = self.accelerator.unwrap_model(self.model)
        generate_output: torch.Tensor = unwrapped_model.generate(
            generation_config=self.generation_config, logits_processor=get_logits_processor(), **batch
        )

        if self.model_args.upcast_layernorm:
            restore_layernorm(self.model, layernorm_params)

        query = batch["input_ids"].detach().cpu()
        response = generate_output[:, batch["input_ids"].size(-1) :].detach().cpu()
        queries, responses = [], []
        for i in range(len(query)):
            query_start_index = (query[i] != self.tokenizer.pad_token_id).nonzero()[0].item()
            response_index = (response[i] != self.tokenizer.pad_token_id).nonzero()

            if len(response_index) == 0:
                response_length = 1  # allow empty response
            else:
                response_length = response_index[-1].item() + 1

            queries.append(query[i, query_start_index:])  # remove padding from left
            responses.append(response[i, :response_length])  # remove padding from right

        return queries, responses

    @torch.no_grad()
    def get_rewards(
        self,
        queries: List[torch.Tensor],
        responses: List[torch.Tensor],
        unwrapped_model: "AutoModelForCausalLMWithValueHead",
    ) -> List[torch.Tensor]:
        r"""
        Computes scores using given reward model.

        Both inputs and outputs are put on CPU.
        """
        if self.finetuning_args.reward_model_type == "api":
            token_ids = [torch.cat((q, r), dim=-1).tolist() for q, r in zip(queries, responses)]
            messages = self.tokenizer.batch_decode(token_ids, skip_special_tokens=True)
            return get_rewards_from_server(self.reward_model, messages)

        if self.finetuning_args.reward_model_type == "lora":
            replace_model(unwrapped_model, target="reward")
            reward_model = self.model
        else:
            reward_model = self.reward_model

        batch = self.prepare_model_inputs(queries, responses)

        with torch.cuda.amp.autocast(dtype=self.model_args.compute_dtype):  # support bf16
            _, _, values = reward_model(**batch, output_hidden_states=True, return_dict=True)

        if getattr(unwrapped_model.config, "model_type", None) == "chatglm":  # assume same architecture
            values = torch.transpose(values, 0, 1)

        rewards = []
        for i in range(values.size(0)):
            end_indexes = (batch["input_ids"][i] != self.tokenizer.pad_token_id).nonzero()
            end_index = end_indexes[-1].item() if len(end_indexes) else 0
            rewards.append(values[i, end_index].float().detach().cpu())  # use fp32 type

        if self.finetuning_args.reward_model_type == "lora":
            replace_model(unwrapped_model, target="default")

        return rewards

    @torch.no_grad()
    def get_rewards_Starling(
        self,
        queries: List[torch.Tensor],
        responses: List[torch.Tensor],
        unwrapped_model: "AutoModelForCausalLMWithValueHead",
        sigmoid_score: False
    ) -> List[torch.Tensor]:
        r"""
        Computes scores using given reward model.

        Both inputs and outputs are put on CPU.
        """
        reward_model = self.reward_model
        responses_str = self.tokenizer.batch_decode(responses, skip_special_tokens=True)
        queries_str = self.tokenizer.batch_decode(queries, skip_special_tokens=True)
        messages = [f"<s>[INST] {query} </s> [/INST] {response} </s>" for response, query in zip(responses_str,queries_str)]
        input_ids = []
        attention_masks = []
        reward_tokenizer = reward_model.tokenizer
        reward_tokenizer.truncation_side = "left"
        encodings_dict = reward_tokenizer(
            messages,
            truncation=True,
            max_length=1024,
            padding="max_length",
            return_tensors="pt",
        ).to(self.current_device)
        input_ids = encodings_dict["input_ids"]
        attention_masks = encodings_dict["attention_mask"]
        with torch.cuda.amp.autocast(dtype=self.model_args.compute_dtype):  # support bf16
            scores = reward_model(input_ids=input_ids, attention_mask=attention_masks)
        rewards = []
        for score in scores:
            if sigmoid_score:
                normalized_score = torch.sigmoid(score)  # normalize score using sigmoid
                rewards.append(normalized_score.float().detach().cpu())  # use fp32 type
            else:
                rewards.append(score.float().detach().cpu())
        print(f'responses:{responses_str[0][:50]}...  score:{scores[0]} reward:{rewards[0]}'.replace('\n',' '))
        return rewards

    @PPODecorators.empty_device_cache()
    def batched_forward_pass(
        self,
        model: "AutoModelForCausalLMWithValueHead",
        queries: torch.Tensor,
        responses: torch.Tensor,
        model_inputs: dict,
        return_logits: Optional[bool] = False,
        response_masks: Optional[torch.Tensor] = None,
    ):
        r"""
        Calculates model outputs in multiple batches.

        Subclass and override to inject custom behavior.
        """
        bs = len(queries)
        fbs = self.config.mini_batch_size
        all_logprobs = []
        all_logits = []
        all_masks = []
        all_values = []

        for i in range(math.ceil(bs / fbs)):
            input_kwargs = {key: value[i * fbs : (i + 1) * fbs] for key, value in model_inputs.items()}
            query_batch = queries[i * fbs : (i + 1) * fbs]
            response_batch = responses[i * fbs : (i + 1) * fbs]
            if response_masks is not None:
                response_masks_batch = response_masks[i * fbs : (i + 1) * fbs]
            input_ids = input_kwargs["input_ids"]
            attention_mask = input_kwargs["attention_mask"]

            with torch.cuda.amp.autocast(dtype=self.model_args.compute_dtype):  # support bf16
                logits, _, values = model(**input_kwargs)

            unwrapped_model: "AutoModelForCausalLMWithValueHead" = self.accelerator.unwrap_model(self.model)
            if getattr(unwrapped_model.config, "model_type", None) == "chatglm":
                values = torch.transpose(values, 0, 1)

            logprobs = logprobs_from_logits(logits[:, :-1, :], input_ids[:, 1:])
            masks = torch.zeros_like(attention_mask)
            masks[:, :-1] = attention_mask[:, 1:]

            for j in range(len(query_batch)):
                start = len(query_batch[j]) - 1
                if attention_mask[j, 0] == 0:  # offset left padding
                    start += attention_mask[j, :].nonzero()[0].item()
                end = start + len(response_batch[j])

                if response_masks is not None:
                    response_masks_batch = torch.cat((torch.zeros_like(query_batch[j]), response_masks_batch[j]))[1:]

                masks[j, :start] = 0
                masks[j, end:] = 0
                if response_masks is not None:
                    masks[j, start:end] = masks[j, start:end] * response_masks_batch[j][start:end]

            if return_logits:
                all_logits.append(logits)
            else:
                del logits

            all_values.append(values)
            all_logprobs.append(logprobs)
            all_masks.append(masks)

        return (
            torch.cat(all_logprobs),
            torch.cat(all_logits)[:, :-1] if return_logits else None,
            torch.cat(all_values)[:, :-1],
            torch.cat(all_masks)[:, :-1],
        )

    def save_model(self, output_dir: Optional[str] = None) -> None:
        r"""
        Saves model checkpoint.

        Subclass and override to inject custom behavior.
        """
        if self.args.should_save:
            try:
                self._save(output_dir, state_dict=self.accelerator.get_state_dict(self.model))
            except ValueError:
                logger.warning(
                    " stage3_gather_16bit_weights_on_model_save=false. Saving the full checkpoint instead,"
                    " use zero_to_fp32.py to recover weights"
                )
                self._save(output_dir, state_dict={})
                remove_dummy_checkpoint(True, output_dir, [WEIGHTS_NAME, SAFE_WEIGHTS_NAME])
                self.model.save_checkpoint(output_dir)
